"""把 AI 填的“数据源别名 / 数据名”解析成磁盘完整路径。

核心目标：AI 不需要知道完整路径，只填 gdb 别名 + 数据集名（或 folder 别名 + shp 名）。
解析失败时抛出的错误信息里会带上所有可用候选，方便 AI 自己纠正后再试。

支持的写法（gdb 参数）：
- "city"                  配置里的别名
- "city/roads"            别名 + 数据集名（数据集名会被拆出来）
- "City.gdb"              相对 search_roots 的名字
- "City.gdb/roads"        名字 + 数据集名
- "D:/gis/City.gdb"       绝对路径（allow_absolute_paths=true 时）
- "gdb_for_city"          别名大小写不敏感
"""

from __future__ import annotations

import difflib
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import GDB_SUFFIXES, SHP_SUFFIX, Settings, Workspace

_WALK_TTL = 20.0  # 目录扫描结果缓存时间（秒），避免每次调用都全盘递归
_WALK_CACHE: Dict[Tuple[str, int], Tuple[float, List[Path]]] = {}
_FILE_CACHE: Dict[Tuple[str, int, str], Tuple[float, List[Path]]] = {}


class ResolutionError(RuntimeError):
    """路径解析失败。消息里已经包含候选清单，可直接展示给用户/AI。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _clean(value: Optional[str]) -> str:
    return str(value or "").strip().strip('"').strip("'")


def is_gdb_name(value: str) -> bool:
    return value.lower().rstrip("\\/").endswith(GDB_SUFFIXES)


def looks_like_path(value: str) -> bool:
    if "/" in value or "\\" in value:
        return True
    if len(value) > 1 and value[1] == ":":
        return True
    return Path(value).suffix.lower() in (".gdb", ".mdb", ".shp")


def _split_parts(value: str) -> List[str]:
    return [p for p in value.replace("/", "\\").split("\\") if p]


def _join_parts(parts: Sequence[str]) -> str:
    return os.sep.join(parts)


def _alias(token: str, settings: Settings) -> Optional[Workspace]:
    return settings.workspace(token)


def walk_dirs(root: Path, depth: int, skip: Optional[Iterable[str]] = None) -> List[Path]:
    """列出 root 下 depth 层以内的目录（不进 .gdb/.mdb 内部），带 TTL 缓存。

    skip 里的目录名会被整体跳过（大小写不敏感），用于自动扫描项目时忽略
    .venv / node_modules 这类噪音目录。
    """
    skip_keys = {s.lower() for s in (skip or ())}
    key = (str(root).lower(), int(depth), "|".join(sorted(skip_keys)))
    now = time.time()
    cached = _WALK_CACHE.get(key)
    if cached and now - cached[0] < _WALK_TTL:
        return cached[1]

    found: List[Path] = []
    root = Path(root)
    if root.is_dir():
        base_depth = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            cur = Path(dirpath)
            found.append(cur)
            if cur.name.lower().endswith(GDB_SUFFIXES):
                dirnames[:] = []  # 地理数据库内部不用看
                continue
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in skip_keys
            ]
            if len(cur.parts) - base_depth >= depth:
                dirnames[:] = []
    _WALK_CACHE[key] = (now, found)
    return found


def _walk_dirs(root: Path, depth: int) -> List[Path]:
    """兼容旧调用（不带 skip）。"""
    return walk_dirs(root, depth)


def _walk_files(root: Path, depth: int, suffix: str) -> List[Path]:
    """列出 root 下 depth 层以内、指定后缀的文件（不进 .gdb/.mdb 内部），带 TTL 缓存。

    depth=0 表示只看 root 本身这一层，depth=2 表示 root 及其下面 2 层子目录。
    """
    key = (str(root).lower(), int(depth), suffix.lower())
    now = time.time()
    cached = _FILE_CACHE.get(key)
    if cached and now - cached[0] < _WALK_TTL:
        return cached[1]

    found: List[Path] = []
    root = Path(root)
    if root.is_dir():
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            cur = Path(dirpath)
            for filename in filenames:
                if filename.lower().startswith(('mcpwork_', 'mcpfailed_', 'mcptrash_')):
                    continue
                if filename.lower().endswith(suffix.lower()):
                    found.append(cur / filename)
            if cur.name.lower().endswith(GDB_SUFFIXES):
                dirnames[:] = []  # 地理数据库内部不用看
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if len(cur.parts) - base_depth >= depth:
                dirnames[:] = []
    _FILE_CACHE[key] = (now, found)
    return found


def _candidate_paths(raw: str, settings: Settings) -> Iterable[Path]:
    """把 raw 展开成若干“可能是绝对路径”的候选。

    顺序很重要：**搜索根在前，服务进程的 cwd 在后**。
    这样 AI 写 "./data/City.gdb" 这类相对路径时，会先按「当前项目目录」解析
    （项目目录就是第一个搜索根），而不是被服务进程碰巧所在的工作目录抢走。
    """
    p = Path(raw)
    if p.is_absolute():
        yield p
        return
    for root in settings.search_roots:
        yield Path(root) / p
    yield Path.cwd() / p


def _first_existing(paths: Iterable[Path], predicate) -> Optional[Path]:
    for p in paths:
        try:
            if predicate(p):
                return p
        except OSError:
            continue
    return None


def _is_gdb_dir(p: Path) -> bool:
    return p.is_dir() and p.name.lower().endswith(GDB_SUFFIXES)


def _is_gdb_file(p: Path) -> bool:
    return p.is_file() and p.name.lower().endswith(GDB_SUFFIXES)


# --------------------------------------------------------------------------
# 错误信息（尽量把候选列出来，AI 看了能自己改）
# --------------------------------------------------------------------------
def _hints_not_found(kind: str, raw: str, settings: Settings, extra: Sequence[str] = ()) -> str:
    lines: List[str] = []
    if kind == "gdb":
        aliases = [f"{w.alias} -> {w.path}" for w in settings.by_type("gdb")]
    elif kind == "shp":
        aliases = [f"{w.alias} -> {w.path}" for w in settings.by_type("shp", "folder")]
    else:
        aliases = [f"{w.alias} [{w.type}] -> {w.path}" for w in settings.workspaces.values()]
    if aliases:
        lines.append("配置里可用的别名：" + "；".join(aliases))
    else:
        lines.append("配置里还没有为该类型配置别名（见 config/datasources.json 的 workspaces）。")
    lines.append("搜索根目录：" + ("；".join(str(r) for r in settings.search_roots) or "（空）"))
    if extra:
        lines.append("相近的候选：" + "；".join(list(extra)[:10]))
    if raw:
        lines.append(f"收到的参数是：{raw!r}")
    lines.append("提示：只需填别名或数据名（例如 \"city\"、\"roads\"），不必填完整路径。")
    return "\n".join(lines)


def _close_names(name: str, pool: Iterable[str]) -> List[str]:
    return difflib.get_close_matches(name, list(pool), n=8, cutoff=0.5)


def _tried_note(raw: str, settings: Settings) -> str:
    """路径写错时的直观提示：服务端把相对路径拼成了哪些绝对路径。"""
    if not looks_like_path(raw):
        return ""
    # 去重但保持顺序（项目根目录碰巧等于 cwd 时会出现重复）
    candidates = list(dict.fromkeys(str(p) for p in _candidate_paths(raw, settings)))
    if not candidates:
        return ""
    lines = ["服务端把相对路径拼成了下面这些路径，但都不存在："]
    lines.extend(f"- {c}" for c in candidates[:6])
    if settings.search_roots:
        lines.append(f"（拼接基准是项目根目录：{settings.search_roots[0]}）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# gdb
# --------------------------------------------------------------------------
def resolve_gdb(value: str, settings: Settings) -> Path:
    raw = _clean(value)
    if not raw:
        raise ResolutionError("参数 gdb 不能为空。\n" + _hints_not_found("gdb", raw, settings))

    ws = _alias(raw, settings)
    if ws is not None:
        if ws.type != "gdb":
            raise ResolutionError(
                f"别名 {ws.alias!r} 的类型是 {ws.type}，不是地理数据库（路径：{ws.path}）。"
                f"读 shapefile 请改用 read_shp_data 工具。"
            )
        if not ws.path.exists():
            raise ResolutionError(
                f"别名 {ws.alias!r} 指向的地理数据库不存在：{ws.path}\n"
                "请检查 config/datasources.json 里的 path，或磁盘是否已挂载。"
            )
        return ws.path

    found = _first_existing(_candidate_paths(raw, settings), lambda p: _is_gdb_dir(p) or _is_gdb_file(p))
    if found:
        return found
    if not settings.allow_absolute_paths and Path(raw).is_absolute():
        raise ResolutionError("配置里 allow_absolute_paths=false，不接受绝对路径，请用别名。")

    # 纯名字：在每个搜索根下找 <name>.gdb（含有限深度的递归）
    # 搜索根按顺序尝试，第一个命中的赢（项目目录在最前面）
    name = raw
    if not is_gdb_name(name):
        name = name + ".gdb"
    for root in settings.search_roots:
        hits: List[Path] = []
        for cand in walk_dirs(root, settings.recursive_search_depth):
            if cand.name.lower() == name.lower() and cand not in hits:
                hits.append(cand)
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ResolutionError(
                f"在 {root} 下找到多个名为 {name!r} 的地理数据库，请在 gdb 参数里写得更明确：\n"
                + "\n".join(f"- {h}" for h in hits[:15])
            )

    known = [w.alias for w in settings.by_type("gdb")] + [
        p.name
        for root in settings.search_roots
        for p in walk_dirs(root, settings.recursive_search_depth)
        if p.name.lower().endswith(GDB_SUFFIXES)
    ]
    note = _tried_note(raw, settings)
    raise ResolutionError(
        f"找不到地理数据库 {raw!r}。\n"
        + (note + "\n" if note else "")
        + _hints_not_found("gdb", raw, settings, _close_names(name, known))
    )


def split_gdb_dataset_ref(value: str, dataset: Optional[str], settings: Settings) -> Tuple[str, str]:
    """把 gdb 参数里可能夹带的要素类名拆出来。

    "Demo.gdb/roads" -> ("Demo.gdb", "roads")；"city/roads" -> ("city", "roads")
    """
    raw = _clean(value)
    ds = _clean(dataset)
    parts = _split_parts(raw)
    if len(parts) > 1:
        for i, part in enumerate(parts):
            if part.lower().endswith(GDB_SUFFIXES):
                if i < len(parts) - 1:
                    return _join_parts(parts[: i + 1]), ds or _join_parts(parts[i + 1 :])
                return _join_parts(parts[: i + 1]), ds
        if _alias(parts[0], settings) is not None:
            return parts[0], ds or _join_parts(parts[1:])
    return raw, ds


# --------------------------------------------------------------------------
# shapefile
# --------------------------------------------------------------------------
def _is_shp_dir(p: Path) -> bool:
    return p.is_dir()


def resolve_shp_folder(value: str, settings: Settings) -> Path:
    """解析 shapefile 所在目录（folder 参数）。"""
    raw = _clean(value)
    if not raw:
        raise ResolutionError("参数 folder 不能为空。\n" + _hints_not_found("shp", raw, settings))

    ws = _alias(raw, settings)
    if ws is not None:
        if ws.type == "gdb":
            raise ResolutionError(
                f"别名 {ws.alias!r} 是地理数据库，不是 shapefile 目录（路径：{ws.path}）。"
                "要读 gdb 请用 read_gdb_data 工具。"
            )
        if not ws.path.exists():
            raise ResolutionError(f"别名 {ws.alias!r} 指向的目录不存在：{ws.path}")
        return ws.path.parent if ws.path.is_file() else ws.path

    if raw.lower().endswith(SHP_SUFFIX):
        direct = _first_existing(_candidate_paths(raw, settings), lambda p: p.is_file())
        if direct:
            return direct.parent
    found = _first_existing(_candidate_paths(raw, settings), _is_shp_dir)
    if found:
        return found
    if not settings.allow_absolute_paths and Path(raw).is_absolute():
        raise ResolutionError("配置里 allow_absolute_paths=false，不接受绝对路径，请用别名。")
    raise ResolutionError(
        f"找不到 shapefile 目录 {raw!r}。\n" + _hints_not_found("shp", raw, settings)
    )


def _find_shp_by_name(name: str, settings: Settings, base: Optional[Path] = None) -> Optional[Path]:
    """按文件名（不带扩展名也行）找 shapefile。

    搜索根按顺序尝试，**第一个命中的赢**（项目目录排在搜索根最前面，
    所以项目里的数据优先于共享目录里的同名数据）。同一个根里撞名才报错。
    """
    if not name.lower().endswith(SHP_SUFFIX):
        name = name + SHP_SUFFIX
    depth = 4 if base is not None else settings.recursive_search_depth
    roots = [base] if base else list(settings.search_roots)
    for root in roots:
        hits: List[Path] = []
        for cand in _walk_files(Path(root), depth, SHP_SUFFIX):
            if cand.name.lower() == name.lower() and cand not in hits:
                hits.append(cand)
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ResolutionError(
                f"在 {root} 下找到多个名为 {name!r} 的 shapefile，请用 folder 参数指定：\n"
                + "\n".join(f"- {h}" for h in hits[:15])
            )
    return None


def split_shp_ref(shp: str, folder: Optional[str], settings: Settings) -> Tuple[str, Optional[str]]:
    """"别名/roads" -> ("roads", "别名")；"roads.shp" -> ("roads.shp", folder)。

    注意：绝对路径（含 "D:\\..."）必须原样保留，否则盘符会被误当成目录名。
    """
    raw = _clean(shp)
    if Path(raw).is_absolute():
        return raw, folder
    parts = _split_parts(raw)
    if len(parts) > 1 and _alias(parts[0], settings) is not None:
        return _join_parts(parts[1:]), parts[0]
    return raw, folder


def resolve_shp_file(shp: str, folder: Optional[str], settings: Settings) -> Path:
    """解析到具体的 .shp 文件。"""
    raw, folder_ref = split_shp_ref(shp, folder, settings)
    if not raw:
        raise ResolutionError("参数 shp 不能为空。\n" + _hints_not_found("shp", raw, settings))

    base: Optional[Path] = resolve_shp_folder(folder_ref, settings) if folder_ref else None

    # 1) 给了（相对/绝对）路径：带不带 .shp 后缀都试一下
    if looks_like_path(raw) or raw.lower().endswith(SHP_SUFFIX):
        names = [raw]
        if not raw.lower().endswith(SHP_SUFFIX):
            names.append(raw + SHP_SUFFIX)  # "./data/shp/roads" 这种写法也认
        for name in names:
            if base is not None:
                cand = base / Path(name).name
                if cand.is_file():
                    return cand
            direct = _first_existing(_candidate_paths(name, settings), lambda p: p.is_file())
            if direct:
                return direct

    # 2) 在给定目录里找
    if base is not None:
        name = raw if raw.lower().endswith(SHP_SUFFIX) else raw + SHP_SUFFIX
        cand = base / name
        if cand.is_file():
            return cand
        hit = _find_shp_by_name(raw, settings, base=base)
        if hit:
            return hit
        pool = [c.name for c in _walk_files(base, 4, SHP_SUFFIX)]
        raise ResolutionError(
            f"在目录 {base} 里找不到 {raw!r}。\n"
            f"该目录下可用的 shapefile：{'；'.join(pool[:20]) or '（没有）'}\n"
            f"相近的候选：{'；'.join(_close_names(raw + SHP_SUFFIX, pool)) or '（无）'}"
        )

    # 3) 在所有搜索根里按名字找
    hit = _find_shp_by_name(raw, settings)
    if hit:
        return hit

    pool = [
        c.name
        for root in settings.search_roots
        for c in _walk_files(Path(root), settings.recursive_search_depth, SHP_SUFFIX)
    ]
    note = _tried_note(raw, settings)
    raise ResolutionError(
        f"找不到 shapefile {raw!r}。\n"
        + (note + "\n" if note else "")
        + _hints_not_found("shp", raw, settings, _close_names(raw + SHP_SUFFIX, pool))
    )


def list_shp_files(folder: str, settings: Settings, recursive: bool = False) -> List[Path]:
    base = resolve_shp_folder(folder, settings)
    files = _walk_files(base, 4 if recursive else 0, SHP_SUFFIX)
    return sorted(files, key=lambda p: str(p).lower())


# --------------------------------------------------------------------------
# 分析工具（会写数据）用的输入 / 输出路径解析
# --------------------------------------------------------------------------
def _inside_gdb(path: Path) -> bool:
    """路径像是 .gdb 内的要素类（文件系统上不存在，交给 arcpy 判断）。"""
    return any(part.lower().endswith(GDB_SUFFIXES) for part in path.parts[:-1])


def resolve_input_path(value: str, settings: Settings, label: str = "输入数据") -> Path:
    """解析一个已存在的输入数据集（矢量 / 栅格 / 表 / gdb 内的要素类）。

    只算路径，不读内容——真正打开由 arcpy 负责。
    """
    raw = _clean(value)
    if not raw:
        raise ResolutionError(f"{label}不能为空。")

    p = Path(raw)
    if p.is_absolute():
        if p.exists() or _inside_gdb(p):
            return p
        raise ResolutionError(f"{label}不存在：{p}")

    for candidate in _candidate_paths(raw, settings):
        if candidate.exists() or _inside_gdb(candidate):
            return candidate

    pool = [
        c.name for root in settings.search_roots
        for c in _walk_files(Path(root), settings.recursive_search_depth, SHP_SUFFIX)
    ]
    note = _tried_note(raw, settings)
    raise ResolutionError(
        f"找不到{label} {raw!r}。\n"
        + (note + "\n" if note else "")
        + _hints_not_found("input", raw, settings, _close_names(raw, pool))
    )


def resolve_input_paths(value: Any, settings: Settings, label: str = "输入数据") -> List[Path]:
    """同 resolve_input_path，但接受单个字符串、逗号分隔字符串或数组（相交/更新要多个输入）。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value if str(v).strip()]
    else:
        raise ResolutionError(f"{label}的写法不对，应该是字符串或字符串数组。")
    if not items:
        raise ResolutionError(f"{label}不能为空。")
    return [resolve_input_path(item, settings, label) for item in items]


def resolve_output_path(value: str, settings: Settings, label: str = "输出路径") -> Path:
    """解析输出路径（可以还不存在）。

    规则与输入一致：以 ./ 开头的相对路径按项目根目录解析。
    这里**不创建目录**——建目录 / 建 gdb 由 arcpy 侧统一负责（见 core/arcpy_worker.py）。
    """
    raw = _clean(value)
    if not raw:
        raise ResolutionError(
            f"{label}不能为空。请给出要写到哪，例如 out_features=\"./output/result.shp\"。"
        )
    p = Path(raw)
    if p.is_absolute():
        return p
    roots = list(settings.search_roots)
    if not roots:
        raise ResolutionError(
            f"无法解析相对{label} {raw!r}：还不知道项目根目录。\n"
            '先调用 use_project(folder="<项目根目录>")，或改用绝对路径。'
        )
    return Path(roots[0]) / p   # 项目根目录就是第一个搜索根
