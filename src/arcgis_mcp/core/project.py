"""把「当前项目目录」变成数据源作用域。

数据源不再写死在全局配置里：服务端每次请求都会先确定**当前项目目录**，
再把该目录下的 gdb / shapefile 自动纳入可用数据源。

一、项目目录怎么确定（优先级从高到低）

1. `--project` 命令行强制指定（适合给某个项目单开一个服务实例）
2. `use_project` 工具在会话里显式指定（AI 主动切换）
3. 请求头 `X-Project-Root`（HTTP 客户端可以按项目配置这一行）
4. 环境变量 `ARCGIS_MCP_PROJECT`
5. stdio 模式下工作进程的 cwd（AI 客户端通常就用打开的项目目录启动 stdio 服务）
6. 全局配置里的 `default_project`
7. HTTP 模式下若开了 `use_server_cwd_as_project`，用服务进程自己的 cwd
   否则：没有项目（只使用全局 workspaces），并在 list_datasources 里说明怎么指定

二、确定项目目录后，数据源按以下层次合并（同名时**上层赢**）

1. 项目配置 `<项目>/.arcgis-mcp/datasources.json` 的 workspaces   origin=project
2. 自动扫描项目目录发现的 gdb / 含 .shp 的目录                      origin=auto
3. 全局配置 `config/datasources.json` 的 workspaces                 origin=global

搜索根 = [项目目录] + 项目配置的 search_roots + 全局 search_roots。
把项目目录本身放进搜索根很关键：这样 `gdb="City.gdb"`、`shp="roads"` 不做任何配置就能用。
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

from .config import (
    GDB_SUFFIXES,
    SHP_SUFFIX,
    ProjectConfig,
    Settings,
    Workspace,
    expand_path,
    load_project_config,
)
from .resolver import walk_dirs

PROJECT_HEADER = "x-project-root"
ENV_PROJECT = "ARCGIS_MCP_PROJECT"


@dataclass
class ProjectView:
    """某个项目目录下「最终生效」的数据源视图。"""

    root: Optional[Path]
    source: str  # cli | session | header | env | cwd | default | server-cwd | none
    settings: Settings  # 已合并好的设置，直接交给 resolver 用
    sources: List[Workspace] = field(default_factory=list)
    shadowed: List[Dict[str, str]] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    project_config: Optional[Path] = None
    built_at: float = 0.0

    @property
    def detected(self) -> bool:
        return self.root is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_root": str(self.root) if self.root else None,
            "project_root_source": self.source,
            "project_config": str(self.project_config) if self.project_config else None,
            "project_config_exists": bool(self.project_config and self.project_config.is_file()),
            "search_roots": [str(p) for p in self.settings.search_roots],
            "output_workspace": str(self.settings.output_workspace) if self.settings.output_workspace else None,
            "auto_create_gdb": self.settings.auto_create_gdb,
            "sources": [w.to_dict() for w in self.sources],
            "problems": self.problems,
            "shadowed": self.shadowed,
        }


# --------------------------------------------------------------------------
# 自动发现
# --------------------------------------------------------------------------
def _has_shp_directly(directory: Path) -> bool:
    try:
        with os.scandir(directory) as entries:
            return any(e.is_file() and e.name.lower().endswith(SHP_SUFFIX) for e in entries)
    except OSError:
        return False


def discover_sources(root: Path, settings: Settings) -> List[Workspace]:
    """扫描项目目录，把 gdb 和含 shapefile 的目录登记成数据源。

    仅依赖文件名，不调用 arcpy，所以很快。
    """
    if not settings.auto_discover or not root.is_dir():
        return []

    depth = settings.auto_discover_depth
    skip = set(settings.auto_discover_skip)
    dirs = walk_dirs(root, depth, skip=skip)

    found: List[Workspace] = []
    for directory in dirs:
        if directory.name.lower().endswith(GDB_SUFFIXES):
            found.append(Workspace(alias=directory.stem, type="gdb", path=directory,
                                   description="自动扫描发现", origin="auto"))
        elif directory == root:
            continue  # 项目根自己不当 shapefile 目录（太宽泛）
        elif _has_shp_directly(directory):
            found.append(Workspace(alias=directory.name, type="shp", path=directory,
                                   description="自动扫描发现", origin="auto"))
    return found


def _assign_alias(ws: Workspace, root: Path, taken: Dict[str, Workspace]) -> Tuple[Workspace, Optional[Workspace]]:
    """给自动发现的源分配别名：优先目录名，冲突时退回相对路径。"""
    primary = ws.alias.strip().lower()
    secondary: Optional[Workspace] = None
    try:
        rel = str(ws.path.relative_to(root)).replace(os.sep, "/")
        # 去掉 .gdb / .mdb 后缀，让 "data/demo" 这种别名更好写
        if rel.lower().rstrip("/").endswith(GDB_SUFFIXES):
            rel = rel[: -len(ws.path.suffix)]
    except ValueError:
        rel = ws.alias

    rel_alias = rel.strip().lower()
    if primary and primary not in taken:
        chosen = primary
    else:
        chosen = rel_alias
        secondary = None if rel_alias == primary else dataclasses.replace(
            ws, alias=rel_alias, description=f"自动扫描发现（相对路径别名，原别名 {primary!r} 已被占用）"
        )
        if rel_alias in taken:
            # 极端情况：连相对路径也撞了，加序号
            index = 2
            while f"{rel_alias}-{index}" in taken:
                index += 1
            chosen = f"{rel_alias}-{index}"
            secondary = None
    result = dataclasses.replace(ws, alias=chosen)
    return result, secondary


def _path_key(path: Path) -> str:
    """用于判断「是不是同一条路径」的规范化 key。"""
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _merge_sources(layers: Sequence[Tuple[str, Sequence[Workspace]]]) -> Tuple[Dict[str, Workspace], List[Dict[str, str]]]:
    """按层合并，先出现的赢；记录被遮蔽的别名。"""
    merged: Dict[str, Workspace] = {}
    shadowed: List[Dict[str, str]] = []
    for origin, items in layers:
        for ws in items:
            key = ws.alias.strip().lower()
            if not key:
                continue
            if key in merged:
                shadowed.append({"alias": key, "used": merged[key].origin, "ignored": origin,
                                 "ignored_path": str(ws.path)})
                continue
            merged[key] = dataclasses.replace(ws, origin=origin)
    return merged, shadowed


# --------------------------------------------------------------------------
# 项目视图注册表
# --------------------------------------------------------------------------
def encode_header_path(folder: str) -> str:
    """把项目路径编码成能放进 HTTP 头的值。

    HTTP 头只能是 ASCII，所以路径里的中文等字符要百分号编码，服务端再解回来。
    纯 ASCII 路径保持原样，可读性更好。
    """
    text = str(folder).replace("\\", "/")
    return quote(text, safe="/:") if any(ord(ch) > 127 for ch in text) else text


def decode_header_path(value: str) -> str:
    """还原 encode_header_path 的结果（对没编码的值是幂等的）。"""
    if "%" not in value:
        return value
    return unquote(value)


class ProjectRegistry:
    def __init__(self, base: Settings, mode: str = "http") -> None:
        self.base = base
        self.mode = mode  # http | stdio
        self._views: Dict[str, Tuple[float, ProjectView]] = {}
        self._session: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._session_output = {}

    # ---- 会话级别的手动指定 ----
    def set_session_project(self, session_key: str, folder: str) -> ProjectView:
        path = expand_path(folder)
        if not path.is_dir():
            raise NotADirectoryError(f"项目目录不存在：{path}")
        with self._lock:
            if self._session.get(session_key) != str(path):
                self._session_output.pop(session_key, None)
            self._session[session_key] = str(path)
            self._views.pop(str(path), None)
        return self.view(session_key=session_key)

    def session_project(self, session_key: str) -> Optional[str]:
        with self._lock:
            return self._session.get(session_key)

    def invalidate(self) -> None:
        with self._lock:
            self._views.clear()

    def set_output_policy(self, session_key, root, output_gdb=None, auto_create_gdb=None):
        changes = {}
        if output_gdb is not None:
            path = expand_path(output_gdb)
            path = path if path.is_absolute() else root / path
            if path.suffix.lower() != '.gdb':
                raise ValueError('output_gdb 必须是 .gdb 路径；只选择工作库，不在此处创建。')
            changes['output_workspace'] = path.resolve()
        if auto_create_gdb is not None:
            changes['auto_create_gdb'] = auto_create_gdb
        with self._lock:
            current = self._session_output.get(session_key, (None, {}))
            policy = dict(current[1]) if current[0] == root else {}
            policy.update(changes)
            self._session_output[session_key] = (root, policy)

    def _with_output_policy(self, view, session_key):
        with self._lock:
            root, changes = self._session_output.get(session_key, (None, {}))
            if root == view.root and changes:
                return dataclasses.replace(view, settings=dataclasses.replace(view.settings, **changes))
        return view

    # ---- 主入口 ----
    def view(self, header: Optional[str] = None, session_key: Optional[str] = None) -> ProjectView:
        root, source, problems = self._detect(header, session_key)
        key = str(root) if root else "__none__"
        now = time.time()
        with self._lock:
            cached = self._views.get(key)
            if cached and now - cached[0] < self.base.project_cache_ttl and not problems:
                return self._with_output_policy(cached[1], session_key)
        view = self._build(root, source)
        view.problems.extend(problems)
        with self._lock:
            self._views[key] = (now, view)
        return self._with_output_policy(view, session_key)

    def _detect(self, header: Optional[str],
                session_key: Optional[str]) -> Tuple[Optional[Path], str, List[str]]:
        base = self.base
        problems: List[str] = []
        if base.forced_project is not None:
            return base.forced_project, "cli", problems
        if session_key:
            manual = self.session_project(session_key)
            if manual:
                return Path(manual), "session", problems
        if header:
            raw = decode_header_path(str(header).strip().strip('"'))
            if raw:
                candidate = expand_path(raw)
                if not candidate.is_absolute():
                    candidate = Path.cwd() / candidate
                if candidate.is_dir():
                    return candidate, "header", problems
                problems.append(
                    f"请求头里的项目目录不存在：{candidate}（请求头值：{header!r}）。"
                    "若路径含中文，客户端需用百分号编码，例如 "
                    f"{encode_header_path(str(candidate))}。"
                )
        env = os.environ.get(ENV_PROJECT)
        if env:
            candidate = expand_path(env)
            if candidate.is_dir():
                return candidate, "env", problems
            problems.append(f"环境变量 {ENV_PROJECT} 指向的目录不存在：{candidate}")
        if self.mode == "stdio":
            cwd = Path.cwd()
            if cwd.is_dir():
                return cwd, "cwd", problems
        if base.default_project is not None:
            return base.default_project, "default", problems
        if self.mode != "stdio" and base.use_server_cwd_as_project:
            cwd = Path.cwd()
            if cwd.is_dir():
                return cwd, "server-cwd", problems
        return None, "none", problems

    def _build(self, root: Optional[Path], source: str) -> ProjectView:
        base = self.base
        problems: List[str] = []

        project_cfg = ProjectConfig(path=Path("."))
        if root is not None:
            project_cfg = load_project_config(root / base.project_config_rel, base_dir=root)
            problems.extend(project_cfg.problems)

        auto: List[Workspace] = []
        if root is not None:
            try:
                raw_auto = discover_sources(root, base)
            except OSError as exc:
                problems.append(f"自动扫描项目目录失败：{exc}")
                raw_auto = []
            # 先合并已占用的别名（项目配置 + 全局），再给自动发现的分配别名
            taken: Dict[str, Workspace] = {}
            taken.update(project_cfg.workspaces)
            for alias, ws in base.workspaces.items():
                taken.setdefault(alias, ws)
            # 已经登记过的路径不再重复列举（否则同一个目录会多出一个 XX-2 别名）
            known_paths = {_path_key(w.path) for w in taken.values()}
            for ws in raw_auto:
                key = _path_key(ws.path)
                if key in known_paths:
                    continue
                assigned, secondary = _assign_alias(ws, root, taken)
                taken.setdefault(assigned.alias.strip().lower(), assigned)
                known_paths.add(key)
                auto.append(assigned)
                if secondary is not None:
                    taken.setdefault(secondary.alias.strip().lower(), secondary)
                    auto.append(secondary)

        merged, shadowed = _merge_sources([
            ("project", list(project_cfg.workspaces.values())),
            ("auto", auto),
            ("global", list(base.workspaces.values())),
        ])

        roots: List[Path] = []
        for candidate in ([root] if root else []) + list(project_cfg.search_roots) + list(base.search_roots):
            if candidate not in roots:
                roots.append(candidate)

        output_workspace = project_cfg.output_workspace or base.output_workspace
        if output_workspace is not None and not output_workspace.is_absolute() and root is not None:
            output_workspace = root / output_workspace
        settings = dataclasses.replace(base, workspaces=merged, search_roots=roots,
            output_workspace=output_workspace,
            auto_create_gdb=(project_cfg.auto_create_gdb if project_cfg.auto_create_gdb is not None
                             else base.auto_create_gdb))
        sources = sorted(merged.values(), key=lambda w: (w.origin != "project", w.origin != "auto", w.alias))

        return ProjectView(
            root=root, source=source, settings=settings, sources=sources,
            shadowed=shadowed, problems=problems, project_config=project_cfg.path,
            built_at=time.time(),
        )


def client_snippets(project_root: str) -> Dict[str, str]:
    """生成「让客户端告诉服务端项目目录」的配置片段，给 --init-project 用。

    路径里的中文会做百分号编码——HTTP 头只能是 ASCII。
    """
    encoded = encode_header_path(project_root)
    plain = str(project_root).replace("\\", "/")
    return {
        "codex_http": (
            "# 写进 ~/.codex/config.toml，或写在项目里的 <项目>/.codex/config.toml\n"
            "experimental_use_rmcp_client = true\n\n"
            "[mcp_servers.arcgis]\n"
            'url = "http://127.0.0.1:8765/mcp"\n'
            f'http_headers = {{ "X-Project-Root" = "{encoded}" }}\n'
            "tool_timeout_sec = 300"
        ),
        "claude_code_http": (
            "claude mcp add --transport http arcgis http://127.0.0.1:8765/mcp "
            f'--header "X-Project-Root: {encoded}"'
        ),
        "stdio_any_client": (
            "# stdio 模式下客户端一般用项目目录作为工作目录，服务端自动识别，不用写路径。\n"
            "# 若客户端不设置 cwd，或用 HTTP 但不想配请求头，就让 AI 调用\n"
            f'# use_project(folder="{plain}")，或者用环境变量兜底：\n'
            f"ARCGIS_MCP_PROJECT={plain}"
        ),
    }
