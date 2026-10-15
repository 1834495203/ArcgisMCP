"""配置加载。

配置文件查找顺序：
1. 命令行 --config 或环境变量 ARCGIS_MCP_CONFIG
2. <项目根>/config/datasources.json

配置里最重要的是 workspaces：给每个 gdb / shapefile 目录起一个短别名，
AI 只需要填别名，不需要知道磁盘路径。
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

def _locate_project_root() -> Path:
    """定位仓库根目录（含 pyproject.toml / scripts 的那一层）。

    刻意不写死「上几级」：模块被挪进 core/ 子包时（本项目重构过一次），
    写死层级会**悄悄**指到 src/，导致默认配置路径和 worker 的 PYTHONPATH 一起错。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() or (parent / "scripts").is_dir():
            return parent
    return here.parent


PROJECT_ROOT = _locate_project_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "datasources.json"
DEFAULT_ARCPY_PYTHON = r"D:\software\arcgispro\bin\Python\envs\arcgispro-py3\python.exe"

GDB_SUFFIXES = (".gdb", ".mdb")
SHP_SUFFIX = ".shp"

# 别名类型归一化：gdb(地理数据库) / shp(shapefile 目录) / folder(普通目录)
_TYPE_ALIASES = {
    "gdb": "gdb",
    "fgdb": "gdb",
    "filegdb": "gdb",
    "file_gdb": "gdb",
    "mdb": "gdb",
    "shp": "shp",
    "shps": "shp",
    "shapefile": "shp",
    "shapefiles": "shp",
    "shp_dir": "shp",
    "shpdir": "shp",
    "folder": "folder",
    "dir": "folder",
    "directory": "folder",
    "workspace": "folder",
}


class ConfigError(RuntimeError):
    """配置有问题（比如路径写错、类型不认识）。"""


def normalize_type(value: Any) -> str:
    key = str(value or "").strip().lower()
    if key not in _TYPE_ALIASES:
        raise ConfigError(
            f"不认识的数据源类型 {value!r}，可用值：gdb（地理数据库）/ shp（shapefile 目录）/ folder（普通目录）"
        )
    return _TYPE_ALIASES[key]


def expand_path(value: Any) -> Path:
    """展开 %VAR% / ~。相对路径保持相对，由调用方决定按哪个基准目录解析。"""
    text = str(value or "").strip().strip('"')
    if not text:
        raise ConfigError("路径不能为空")
    return Path(os.path.expandvars(os.path.expanduser(text)))


@dataclass(frozen=True)
class Workspace:
    alias: str
    type: str  # gdb | shp | folder
    path: Path
    description: str = ""
    origin: str = "config"  # config | project | auto —— 这个别名是哪一层来的

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alias": self.alias,
            "type": self.type,
            "path": str(self.path),
            "origin": self.origin,
            "description": self.description,
            "exists": self.exists,
        }


@dataclass
class Settings:
    config_path: Path
    arcpy_python: str = DEFAULT_ARCPY_PYTHON
    search_roots: List[Path] = field(default_factory=list)
    allow_absolute_paths: bool = True
    default_limit: int = 100
    max_limit: int = 2000
    max_response_bytes: int = 1_000_000
    recursive_search_depth: int = 3
    call_timeout: float = 300.0
    startup_timeout: float = 180.0
    read_timeout: float = 60.0
    job_wait_seconds: float = 0.0
    queue_timeout: float = 60.0
    tool_timeouts: Dict[str, float] = field(default_factory=lambda: {
        'eliminate': 1800.0, 'union': 1800.0, 'intersect': 1800.0, 'dissolve': 3600.0,
    })
    workspaces: Dict[str, Workspace] = field(default_factory=dict)

    # ---- 项目作用域相关 -------------------------------------------
    # 数据源不再写死在全局配置里：服务端会先确定「当前项目目录」，
    # 再自动扫描该目录下的 gdb / shp，并叠加项目自己的配置文件。
    forced_project: Optional[Path] = None      # --project 强制指定
    default_project: Optional[Path] = None     # 配置里的兜底项目目录
    project_config_rel: str = ".arcgis-mcp/datasources.json"
    auto_discover: bool = True
    auto_discover_depth: int = 3
    auto_discover_skip: List[str] = field(default_factory=lambda: [
        ".venv", "venv", "node_modules", "build", "dist", "__pycache__",
        "site-packages", ".idea", ".vscode", "target", "bin", "obj",
    ])
    project_cache_ttl: float = 30.0
    use_server_cwd_as_project: bool = False    # HTTP 模式下是否拿服务进程 cwd 当项目
    require_project: bool = True               # 没确定项目根目录时，是否拒绝执行读数据的工具
    allow_arcpy_code: bool = False             # 是否开放「在 arcpy 里执行任意代码」这个工具

    # ---- 查询辅助 -------------------------------------------------
    def workspace(self, alias: str) -> Optional[Workspace]:
        return self.workspaces.get(str(alias or "").strip().lower())

    def by_type(self, *types: str) -> List[Workspace]:
        wanted = {t.lower() for t in types}
        return [w for w in self.workspaces.values() if w.type in wanted]

    @property
    def arcpy_python_path(self) -> Path:
        return expand_path(self.arcpy_python)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "arcpy_python": self.arcpy_python,
            "arcpy_python_exists": self.arcpy_python_path.exists(),
            "search_roots": [str(p) for p in self.search_roots],
            "allow_absolute_paths": self.allow_absolute_paths,
            "default_limit": self.default_limit,
            "max_limit": self.max_limit,
            "recursive_search_depth": self.recursive_search_depth,
            "forced_project": str(self.forced_project) if self.forced_project else None,
            "default_project": str(self.default_project) if self.default_project else None,
            "project_config_rel": self.project_config_rel,
            "auto_discover": self.auto_discover,
            "use_server_cwd_as_project": self.use_server_cwd_as_project,
            "require_project": self.require_project,
            "allow_arcpy_code": self.allow_arcpy_code,
            'call_timeout': self.call_timeout,
            'startup_timeout': self.startup_timeout,
            'read_timeout': self.read_timeout,
            'job_wait_seconds': self.job_wait_seconds,
            'queue_timeout': self.queue_timeout,
            'tool_timeouts': self.tool_timeouts,
            "workspaces": [w.to_dict() for w in self.workspaces.values()],
        }


def _parse_workspaces(raw: Any) -> Dict[str, Workspace]:
    """同时支持 dict 形式 {别名: {...}} 和 list 形式 [{alias, type, path}]。"""
    items: List[Dict[str, Any]] = []
    if raw is None:
        return {}
    if isinstance(raw, dict):
        for alias, body in raw.items():
            if isinstance(body, str):  # "别名": "D:/x.gdb" 简写
                items.append({"alias": alias, "type": _guess_type(body), "path": body})
            elif isinstance(body, dict):
                item = dict(body)
                item.setdefault("alias", alias)
                item.setdefault("type", _guess_type(item.get("path", "")))
                items.append(item)
            else:
                raise ConfigError(f"数据源 {alias!r} 的配置格式不对，应为对象或路径字符串")
    elif isinstance(raw, list):
        for body in raw:
            if not isinstance(body, dict):
                raise ConfigError("workspaces 列表里每一项都必须是对象")
            item = dict(body)
            item.setdefault("type", _guess_type(item.get("path", "")))
            items.append(item)
    else:
        raise ConfigError("workspaces 必须是对象或数组")

    result: Dict[str, Workspace] = {}
    for item in items:
        alias = str(item.get("alias") or "").strip()
        if not alias:
            raise ConfigError(f"数据源缺少 alias 字段：{item!r}")
        if "path" not in item:
            raise ConfigError(f"数据源 {alias!r} 缺少 path 字段")
        ws = Workspace(
            alias=alias,
            type=normalize_type(item.get("type")),
            path=expand_path(item["path"]),
            description=str(item.get("description") or ""),
        )
        key = alias.lower()
        if key in result:
            raise ConfigError(f"数据源别名重复（别名不区分大小写）：{alias}")
        result[key] = ws
    return result


def _guess_type(path: Any) -> str:
    text = str(path or "").strip().lower().rstrip("\\/")
    if text.endswith(".gdb") or text.endswith(".mdb"):
        return "gdb"
    return "shp"


def _parse_roots(raw: Any) -> List[Path]:
    """全局搜索根。默认留空：正常情况下项目目录会自动成为搜索根。"""
    if raw is None:
        return []
    if isinstance(raw, (str, Path)):
        raw = [raw]
    if not isinstance(raw, list):
        raise ConfigError("search_roots 必须是数组或字符串")
    return [expand_path(p) for p in raw]


def _parse_optional_path(raw: Any) -> Optional[Path]:
    if raw in (None, ""):
        return None
    return expand_path(raw)


def load_settings(config_path: Optional[Union[str, os.PathLike]] = None) -> Settings:
    explicit = config_path is not None
    path = Path(config_path or os.environ.get("ARCGIS_MCP_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()

    raw: Dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{path} -> {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件顶层必须是对象：{path}")
    elif explicit:
        raise ConfigError(f"配置文件不存在：{path}")

    limits = raw.get("defaults") or {}
    if not isinstance(limits, dict):
        raise ConfigError("defaults 必须是对象")

    settings = Settings(
        config_path=path,
        arcpy_python=str(
            os.environ.get("ARCGIS_MCP_ARCPY_PYTHON")
            or raw.get("arcpy_python")
            or DEFAULT_ARCPY_PYTHON
        ),
        search_roots=_parse_roots(raw.get("search_roots")),
        allow_absolute_paths=bool(raw.get("allow_absolute_paths", True)),
        default_limit=int(limits.get("limit", 100)),
        max_limit=int(limits.get("max_limit", 2000)),
        max_response_bytes=int(limits.get("max_response_bytes", 1_000_000)),
        recursive_search_depth=int(raw.get("recursive_search_depth", 3)),
        call_timeout=float(raw.get("call_timeout", 300.0)),
        startup_timeout=float(raw.get("startup_timeout", 180.0)),
        read_timeout=float(raw.get('read_timeout', 60.0)),
        job_wait_seconds=float(raw.get('job_wait_seconds', 0.0)),
        queue_timeout=float(raw.get('queue_timeout', 60.0)),
        tool_timeouts={**{'eliminate': 1800.0, 'union': 1800.0, 'intersect': 1800.0, 'dissolve': 3600.0},
                       **{k: float(v) for k, v in raw.get('tool_timeouts', {}).items()}},
        workspaces=_parse_workspaces(raw.get("workspaces")),
        forced_project=_parse_optional_path(os.environ.get("ARCGIS_MCP_PROJECT")),
        default_project=_parse_optional_path(raw.get("default_project")),
        project_config_rel=str(raw.get("project_config_rel") or ".arcgis-mcp/datasources.json"),
        auto_discover=bool(raw.get("auto_discover", True)),
        auto_discover_depth=int(raw.get("auto_discover_depth", 3)),
        project_cache_ttl=float(raw.get("project_cache_ttl", 30.0)),
        use_server_cwd_as_project=bool(raw.get("use_server_cwd_as_project", False)),
        require_project=bool(raw.get("require_project", True)),
        allow_arcpy_code=bool(raw.get("allow_arcpy_code", False)),
    )
    skip = raw.get("auto_discover_skip")
    if isinstance(skip, list) and skip:
        settings.auto_discover_skip = [str(s) for s in skip]
    for key in ('call_timeout', 'startup_timeout', 'read_timeout', 'queue_timeout'):
        value = getattr(settings, key)
        if not math.isfinite(value) or value <= 0:
            raise ConfigError(key + ' 必须是大于 0 的有限秒数')
    if not math.isfinite(settings.job_wait_seconds) or settings.job_wait_seconds < 0:
        raise ConfigError('job_wait_seconds 必须是大于等于 0 的有限秒数')
    if any(not math.isfinite(v) or v <= 0 for v in settings.tool_timeouts.values()):
        raise ConfigError('tool_timeouts 中的秒数必须大于 0 且有限')
    return settings


def _resolve_against_base(path: Path, base: Optional[Path]) -> Path:
    """相对路径按项目目录解析，这样项目配置可以直接提交到仓库、换机器也能用。"""
    if path.is_absolute() or base is None:
        return path
    return base / path


def load_project_config(path: Path, base_dir: Optional[Path] = None) -> "ProjectConfig":
    """读取项目级配置 <项目>/.arcgis-mcp/datasources.json。

    里面写相对路径时，按 base_dir（项目目录）解析。
    这里刻意不抛异常：项目配置写坏了不应该让整个服务挂掉，
    只把它记成 problem，由工具把问题告诉 AI / 用户。
    """
    problems: List[str] = []
    workspaces: Dict[str, Workspace] = {}
    roots: List[Path] = []
    if not path.is_file():
        return ProjectConfig(path=path, workspaces=workspaces, search_roots=roots, problems=problems)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"项目配置无法解析：{path} -> {exc}")
        return ProjectConfig(path=path, workspaces=workspaces, search_roots=roots, problems=problems)
    if not isinstance(raw, dict):
        problems.append(f"项目配置顶层必须是对象：{path}")
        return ProjectConfig(path=path, workspaces=workspaces, search_roots=roots, problems=problems)

    try:
        workspaces = {
            alias: dataclasses.replace(
                ws, origin="project", path=_resolve_against_base(ws.path, base_dir)
            )
            for alias, ws in _parse_workspaces(raw.get("workspaces")).items()
        }
    except ConfigError as exc:
        problems.append(f"项目配置 workspaces 有问题：{exc}")
    try:
        roots = [_resolve_against_base(p, base_dir) for p in _parse_roots(raw.get("search_roots"))]
    except ConfigError as exc:
        problems.append(f"项目配置 search_roots 有问题：{exc}")
    return ProjectConfig(path=path, workspaces=workspaces, search_roots=roots, problems=problems)


@dataclass
class ProjectConfig:
    path: Path
    workspaces: Dict[str, Workspace] = field(default_factory=dict)
    search_roots: List[Path] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.path.is_file()


def self_check(settings: Settings) -> List[str]:
    """返回配置层面的问题列表（空列表表示没问题）。"""
    problems: List[str] = []
    if not settings.arcpy_python_path.exists():
        problems.append(
            f"arcpy Python 不存在：{settings.arcpy_python}（请在配置里改 arcpy_python）"
        )
    if not settings.workspaces and not settings.search_roots and settings.forced_project is None:
        # 这不是问题：数据源默认由项目目录自动提供，全局配置留空是常态
        pass
    for ws in settings.workspaces.values():
        if not ws.exists:
            problems.append(f"数据源别名 {ws.alias!r} 指向的路径不存在：{ws.path}")
    if settings.forced_project is not None and not settings.forced_project.is_dir():
        problems.append(f"强制指定的项目目录不存在：{settings.forced_project}")
    if settings.default_project is not None and not settings.default_project.is_dir():
        problems.append(f"default_project 指向的目录不存在：{settings.default_project}")
    return problems
