"""arcpy 工作进程（必须用 ArcGIS Pro 自带的 Python 运行）。

ArcGIS Pro 3.x 的 arcpy 是 cp39 的二进制扩展，只能在自带的 Python 3.9 里 import；
而官方 mcp SDK 需要 Python >= 3.10。所以这里把 arcpy 侧独立成一个**常驻子进程**：

    MCP 服务端(Python 3.12)  --stdin/stdout JSON 行-->  本进程(Python 3.9 + arcpy)

协议：
    父 -> 子：{"id": 1, "op": "read", "args": {...}}
    子 -> 父：##ARCGIS_MCP##{"id": 1, "ok": true, "result": {...}}
非 `##ARCGIS_MCP##` 开头的 stdout 输出都是 arcpy 自己的打印，父进程只当日志转发。

本文件只负责：协议、读取类操作（列目录 / 读记录 / 描述）、以及把会写数据的操作
分派给 ops/ 包（那里按功能域分模块，只能依赖标准库和 arcpy）。

注意：本文件是**按文件路径**启动的（不是 -m 包名），因为 core/__init__.py 会连带
import mcp SDK，而这里没有 mcp。同理 ops/ 里也不能 import 包内其他模块。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ops 与本文件同目录，脚本运行时该目录会在 sys.path 里
from ops import EDITING, GEOPROCESSING, META
from ops import safety
from history import History, DATA_POLICY_VERSION
from ops.common import (
    GDB_SUFFIXES,
    SHP_SUFFIX,
    WorkerError,
    brief,
    count_where,
    delete_dataset,
    describe_dataset,
    ensure_output_container,
    field_names,
    jsonable,
    path_exists,
    path_key,
    where_hint,
)

# 本文件的目录（core/）在 sys.path[0] 上，这会让它里面的模块名遮蔽标准库
# （例如曾经有 core/logging.py，导致 arcpy 内部脚本 import logging 拿到错的文件）。
# ops 已经在这之前 import 完了，所以把脚本目录挪到末尾：标准库优先，ops 仍然找得到。
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
with contextlib.suppress(ValueError):
    sys.path.remove(_SCRIPT_DIR)
    sys.path.append(_SCRIPT_DIR)

SENTINEL = "##ARCGIS_MCP##"
PROTOCOL_STDOUT = sys.stdout  # 协议专用，绝不能被 arcpy 的输出污染
GEOMETRY_FORMATS = ("none", "wkt", "xy")


# --------------------------------------------------------------------------
# 协议
# --------------------------------------------------------------------------
def send(payload: Dict[str, Any]) -> None:
    PROTOCOL_STDOUT.write(SENTINEL + json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    PROTOCOL_STDOUT.flush()


# --------------------------------------------------------------------------
# 数据集解析（在 gdb / shapefile 目录内部找目标）
# --------------------------------------------------------------------------
def _list_gdb_items(gdb: str, include_rasters: bool = True) -> List[Dict[str, Any]]:
    import arcpy

    items: List[Dict[str, Any]] = []
    previous = arcpy.env.workspace
    arcpy.env.workspace = gdb
    try:
        for name in arcpy.ListFeatureClasses() or []:
            items.append({"name": name, "relative_path": name, "feature_dataset": None,
                          "dataset_type": "FeatureClass"})
        for ds in arcpy.ListDatasets(feature_type="feature") or []:
            for name in arcpy.ListFeatureClasses(feature_dataset=ds) or []:
                items.append({"name": name, "relative_path": f"{ds}/{name}",
                              "feature_dataset": ds, "dataset_type": "FeatureClass"})
        for name in arcpy.ListTables() or []:
            items.append({"name": name, "relative_path": name, "feature_dataset": None,
                          "dataset_type": "Table"})
        if include_rasters:
            for ds in arcpy.ListDatasets(feature_type="raster") or []:
                items.append({"name": ds, "relative_path": ds, "feature_dataset": None,
                              "dataset_type": "RasterDataset"})
            for name in arcpy.ListRasters() or []:
                items.append({"name": name, "relative_path": name, "feature_dataset": None,
                              "dataset_type": "Raster"})
    finally:
        arcpy.env.workspace = previous

    # ArcPy can list the same raster through both ListDatasets and ListRasters.
    items = list({item['relative_path'].replace('\\', '/').lower(): item
                  for item in items if not safety.internal(item['relative_path'])}.values())
    for item in items:
        full = os.path.join(gdb, item["relative_path"].replace("/", os.sep))
        with contextlib.suppress(Exception):
            info = describe_dataset(full)
            item["path"] = full
            for key in ("data_type", "shape_type", "spatial_reference", "record_count"):
                if info.get(key) is not None:
                    item[key] = info[key]
            item.setdefault("data_type", item["dataset_type"])
    return items


def _find_in_gdb(gdb: str, dataset: str) -> Tuple[str, Dict[str, Any]]:
    """在 gdb 里按名字找要素类/表，返回 (完整路径, 命中的列举项)。"""
    if not dataset:
        raise WorkerError(
            f"读取 gdb 时必须给出 dataset（要素类/表的名字）。"
            f"Tips：先用 list_gdb_datasets 查看 {os.path.basename(gdb)} 里有哪些数据。"
        )

    wanted = dataset.strip().strip('"').replace("\\", "/")
    parts = [p for p in wanted.split("/") if p]
    if len(parts) > 1 and parts[0].lower().endswith(GDB_SUFFIXES):
        parts = parts[1:]
    wanted = "/".join(parts)
    leaf = parts[-1] if parts else wanted

    # Exact paths need no full-catalog scan (which describes/counts every dataset).
    import arcpy
    if '..' not in parts and ':' not in wanted and not safety.internal(wanted):
        direct = os.path.join(gdb, *parts)
        if arcpy.Exists(direct):
            desc = arcpy.Describe(direct)
            if str(desc.dataType).lower() in ('featureclass', 'table', 'rasterdataset', 'raster'):
                return direct, {'name': desc.name, 'relative_path': wanted,
                                'path': direct, 'data_type': desc.dataType}

    items = _list_gdb_items(gdb)
    readable = items
    lower = wanted.lower()

    exact = [i for i in readable if i["relative_path"].lower() == lower]
    if not exact:
        exact = [i for i in readable
                 if i["name"].lower() == lower or i["name"].lower() == leaf.lower()]
    if not exact:
        exact = [i for i in readable if i["relative_path"].lower().endswith("/" + lower)]

    if len(exact) > 1:
        choices = "\n".join(f"- {i['relative_path']}" for i in exact[:15])
        raise WorkerError(
            f"要素类 {leaf!r} 在 {os.path.basename(gdb)} 里有多个同名对象，"
            f"请把 dataset 写成完整相对路径：\n{choices}"
        )
    if exact:
        return exact[0]["path"], exact[0]

    available = [i["relative_path"] for i in readable]
    if not available:
        rasters = [i["name"] for i in items if i.get("data_type") in ("Raster", "RasterDataset")]
        note = f"（该 gdb 里只有栅格：{'；'.join(rasters[:10])}）" if rasters else ""
        raise WorkerError(f"{os.path.basename(gdb)} 里没有可读的要素类或表。{note}")

    import difflib

    near = difflib.get_close_matches(wanted, available, n=8, cutoff=0.4)
    raise WorkerError(
        f"{os.path.basename(gdb)} 里找不到 {dataset!r}。\n"
        f"可用的数据（最多列 30 个）：{'；'.join(available[:30])}\n"
        f"相近的名字：{'；'.join(near) if near else '（无）'}"
    )


def _find_shp_in_dir(folder: str, dataset: str) -> Tuple[str, Dict[str, Any]]:
    base = Path(folder)

    def make(path: Path) -> Dict[str, Any]:
        return {"name": path.stem, "relative_path": str(path.relative_to(base)).replace(os.sep, "/"),
                "path": str(path), "data_type": "ShapeFile", "feature_dataset": None}

    if dataset:
        wanted = dataset.strip().strip('"').replace("\\", "/")
        if wanted.lower().endswith(SHP_SUFFIX):
            wanted = wanted[: -len(SHP_SUFFIX)]
        target = wanted + SHP_SUFFIX
        direct = base / target.replace("/", os.sep)
        if direct.is_file():
            return str(direct), make(direct)
        lower = target.lower()
        for path in base.rglob("*"):
            if path.is_file() and path.name.lower() == lower:
                return str(path), make(path)
        for path in base.rglob("*"):
            if (path.is_file() and path.stem.lower() == Path(target).stem.lower()
                    and path.suffix.lower() == ".shp"):
                return str(path), make(path)
        pool = sorted(p.stem for p in base.rglob("*.shp"))
        import difflib

        near = difflib.get_close_matches(Path(target).stem, pool, n=8, cutoff=0.4)
        raise WorkerError(
            f"目录 {base} 里找不到 {dataset!r}。\n"
            f"可用的 shapefile（最多列 30 个）：{'；'.join(pool[:30]) or '（没有）'}\n"
            f"相近的名字：{'；'.join(near) if near else '（无）'}"
        )

    shps = sorted(base.glob("*.shp"))
    if len(shps) == 1:
        return str(shps[0]), make(shps[0])
    if not shps:
        raise WorkerError(f"目录 {base} 里没有 .shp 文件。")
    names = "；".join(p.stem for p in shps[:30])
    raise WorkerError(f"目录 {base} 里有多个 shapefile，请指定 shp 名字：{names}")


def resolve_dataset(workspace: str, dataset: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    ws = Path(str(workspace))
    if not ws.exists():
        raise WorkerError(f"workspace 不存在：{ws}")

    if ws.is_file():
        if ws.suffix.lower() == SHP_SUFFIX:
            return str(ws), {"name": ws.stem, "relative_path": ws.name, "path": str(ws),
                             "data_type": "ShapeFile"}
        if ws.suffix.lower() in GDB_SUFFIXES:
            return _find_in_gdb(str(ws), dataset or "")
        return str(ws), {"name": ws.name, "relative_path": ws.name, "path": str(ws),
                         "data_type": "File"}

    if ws.name.lower().endswith(GDB_SUFFIXES):
        return _find_in_gdb(str(ws), dataset or "")
    return _find_shp_in_dir(str(ws), dataset or "")


# --------------------------------------------------------------------------
# 读取类操作
# --------------------------------------------------------------------------
def op_ping() -> Dict[str, Any]:
    import arcpy

    info = arcpy.GetInstallInfo()
    return {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "arcpy_version": info.get("Version"),
        'data_policy_version': DATA_POLICY_VERSION,
        "arcgis_product": info.get("ProductName"),
        "arcgis_install_dir": info.get("InstallDir"),
        "pid": os.getpid(),
        "operations": {
            "geoprocessing": sorted(GEOPROCESSING),
            "editing": sorted(EDITING),
            "meta": sorted(META),
        },
        "arcpy_tool_count": len(arcpy.ListTools("*")),
    }


def op_list_datasets(workspace: str, recursive: bool = False,
                     include_rasters: bool = True) -> Dict[str, Any]:
    import arcpy

    ws = Path(str(workspace))
    if not ws.exists():
        raise WorkerError(f"workspace 不存在：{ws}")

    if ws.is_dir() and ws.name.lower().endswith(GDB_SUFFIXES):
        items = _list_gdb_items(str(ws), include_rasters=include_rasters)
        return {"workspace": str(ws), "workspace_type": "gdb", "count": len(items), "items": items}

    if ws.is_dir():
        pattern = "**/*.shp" if recursive else "*.shp"
        files = sorted(p for p in ws.glob(pattern) if not safety.internal(p))
        items = []
        for path in files:
            item: Dict[str, Any] = {
                "name": path.stem,
                "relative_path": str(path.relative_to(ws)).replace(os.sep, "/"),
                "path": str(path),
                "data_type": "ShapeFile",
            }
            with contextlib.suppress(Exception):
                info = describe_dataset(str(path))
                for key in ("shape_type", "spatial_reference", "record_count"):
                    if info.get(key) is not None:
                        item[key] = info[key]
            items.append(item)
        return {"workspace": str(ws), "workspace_type": "shp", "count": len(items), "items": items}

    if ws.is_file() and ws.suffix.lower() == SHP_SUFFIX:
        return {"workspace": str(ws), "workspace_type": "shp_file", "count": 1,
                "items": [{"name": ws.stem, "relative_path": ws.name, "path": str(ws),
                           "data_type": "ShapeFile"}]}

    raise WorkerError(f"无法列举的数据源：{ws}（需要 .gdb 目录或 shapefile 目录）")


def op_describe(path: str) -> Dict[str, Any]:
    """描述任意数据集（矢量 / 表 / 栅格 / 文件），不读记录。"""
    target = str(path)
    if not path_exists(target):
        raise WorkerError(f"数据集不存在或打不开：{target}")
    info = describe_dataset(target)
    return info


def _field_meta(field: Any) -> Dict[str, Any]:
    return {
        "name": field.name,
        "type": field.type,
        "alias": field.aliasName,
        "length": field.length,
        "nullable": field.isNullable,
        "precision": getattr(field, "precision", None),
        "scale": getattr(field, "scale", None),
    }


def op_read(
    workspace: str,
    dataset: Optional[str] = None,
    where: Optional[str] = None,
    fields: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    geometry: str = "none",
    order_by: Optional[str] = None,
) -> Dict[str, Any]:
    import arcpy

    arcpy.env.overwriteOutput = True

    path, hit = resolve_dataset(workspace, dataset)
    geom_fmt = str(geometry or "none").strip().lower()
    if geom_fmt not in GEOMETRY_FORMATS:
        raise WorkerError(f"geometry 只能是 {'/'.join(GEOMETRY_FORMATS)}，收到 {geometry!r}")
    limit = int(limit) if limit is not None else 100
    offset = max(int(offset or 0), 0)
    if limit < 0:
        raise WorkerError("limit 不能为负数；limit=0 表示只返回表结构不返回记录。")
    if order_by and ";" in order_by:
        raise WorkerError("order_by 不能包含分号")

    desc = arcpy.Describe(path)
    is_raster = str(desc.dataType).lower() in ('raster', 'rasterdataset', 'rasterband')
    if is_raster and not getattr(desc, 'hasRAT', getattr(arcpy.Raster(path), 'hasRAT', False)):
        raise WorkerError('此栅格没有可读的属性表；仅支持已有属性表的整数分类栅格，不会自动修改源栅格。')
    is_table = is_raster or str(desc.dataType).lower() in ("table", "dbasetable", "textfile")
    all_fields = list(arcpy.ListFields(path))
    by_name = {f.name.lower(): f for f in all_fields}
    value_fields = [f for f in all_fields if f.type != "Geometry"]

    selected: List[Any] = value_fields
    if fields:
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.replace(";", ",").split(",") if f.strip()]
        wanted: List[Any] = []
        missing: List[str] = []
        for name in fields:
            key = str(name).strip().lower()
            if key not in by_name:
                missing.append(str(name))
            elif by_name[key].type != "Geometry":
                wanted.append(by_name[key])
            else:
                missing.append(f"{name}(几何字段请用 geometry 参数控制)")
        if missing:
            raise WorkerError(
                f"字段不存在：{'；'.join(missing)}。\n可用字段：{'；'.join(f.name for f in all_fields)}"
            )
        selected = wanted
        if not selected:
            raise WorkerError("fields 里没有可用的非几何字段。")

    total_count = int(arcpy.management.GetCount(path)[0])
    matched_count = total_count
    if where:
        try:
            matched_count = count_where(path, where, is_table)
        except Exception as exc:
            raise WorkerError(
                f"where 条件无法执行：{where!r}\n{exc}\n"
                f"该数据集的字段有：{'、'.join(f.name for f in all_fields)}\n"
                "提示：字段名要用真实字段名（不是别名），字符串用单引号，例如 NAME = '北京'"
            ) from exc

    tokens: List[str] = [f.name for f in selected]
    geom_tokens: List[str] = []
    if geom_fmt != "none" and not is_table:
        if geom_fmt == "wkt":
            geom_tokens = ["SHAPE@WKT"]
        else:
            geom_tokens = ["SHAPE@X", "SHAPE@Y"]
            if getattr(desc, "hasZ", False):
                geom_tokens.append("SHAPE@Z")
            if getattr(desc, "hasM", False):
                geom_tokens.append("SHAPE@M")
        tokens = geom_tokens + tokens

    records: List[Dict[str, Any]] = []
    skipped = 0
    truncated = False
    sql_clause = (None, f"ORDER BY {order_by}") if order_by else None
    if limit > 0 and matched_count > 0:
        with arcpy.da.SearchCursor(path, tokens, where, sql_clause=sql_clause) as cursor:
            for row in cursor:
                if skipped < offset:
                    skipped += 1
                    continue
                if len(records) >= limit:
                    truncated = True
                    break
                values = dict(zip(tokens, row))
                record: Dict[str, Any] = {}
                if geom_tokens:
                    if geom_fmt == "wkt":
                        record["geometry_wkt"] = values.get("SHAPE@WKT")
                    else:
                        record["geometry"] = {
                            k.replace("SHAPE@", "").lower(): jsonable(values.get(k))
                            for k in geom_tokens}
                for field in selected:
                    record[field.name] = jsonable(values.get(field.name))
                records.append(record)
    truncated = truncated or (offset + len(records) < matched_count and limit > 0)

    result: Dict[str, Any] = {
        "path": path,
        "dataset": hit.get("name"),
        "relative_path": hit.get("relative_path"),
        "data_type": desc.dataType,
        "is_table": is_table,
        'is_raster_attribute_table': is_raster,
        "fields": [_field_meta(f) for f in all_fields],
        "total_count": total_count,
        "matched_count": matched_count,
        "returned": len(records),
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "records": records,
    }
    if not is_table:
        shape_type = getattr(desc, "shapeType", None)
        if shape_type:
            result["shape_type"] = shape_type
        sr = getattr(desc, "spatialReference", None)
        if sr is not None:
            result["spatial_reference"] = {"name": getattr(sr, "name", None),
                                           "wkid": getattr(sr, "factoryCode", None),
                                           "type": getattr(sr, "type", None)}
        with contextlib.suppress(Exception):
            ext = desc.extent
            result["extent"] = {"xmin": ext.XMin, "ymin": ext.YMin, "xmax": ext.XMax,
                                "ymax": ext.YMax,
                                "spatial_reference": ext.spatialReference.name}
        result["has_z"] = bool(getattr(desc, "hasZ", False))
        result["has_m"] = bool(getattr(desc, "hasM", False))
    return result


# --------------------------------------------------------------------------
# 会写数据的操作：分派给 ops/
# --------------------------------------------------------------------------
def _op_geoprocess(
    operation: str,
    inputs: Optional[Sequence[str]] = None,
    output: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """跑一个 in → out 的操作，输入输出都已经是绝对路径。"""
    import time

    import arcpy

    arcpy.env.overwriteOutput = False

    spec = GEOPROCESSING.get(str(operation))
    if spec is None:
        raise WorkerError(f"不支持的操作 {operation!r}，可用：{sorted(GEOPROCESSING)}")
    params = dict(params or {})
    unknown = set(params) - set(spec["allowed"])
    if unknown:
        raise WorkerError(f"{operation} 不认识这些参数：{sorted(unknown)}；"
                          f"可用参数：{sorted(spec['allowed']) or '（无）'}")

    in_paths = [str(p) for p in (inputs or [])]
    if len(in_paths) < spec["min_inputs"]:
        raise WorkerError(f"{operation} 至少需要 {spec['min_inputs']} 个输入，"
                          f"收到 {len(in_paths)} 个")
    for path in in_paths:
        if not path_exists(path):
            raise WorkerError(f"输入不存在或打不开：{path}")
        if not Path(path).exists() and Path(path).suffix.lower() in GDB_SUFFIXES:
            raise WorkerError(f"输入是地理数据库本身，不是里面的数据集：{path}")

    out = str(output or "").strip()
    if not out:
        raise WorkerError("必须指定输出路径")

    # 输出和输入是同一个数据集时绝不继续：overwrite 会先删「输出」，那正是输入数据。
    if any(path_key(out) == path_key(p) for p in in_paths):
        raise WorkerError(
            f"输出和输入指向同一个数据集：{out}\n"
            "那样会把原始数据删掉再重算，所以服务端直接拦下。\n"
            "请把 out_features 换成新名字；要原地改属性请用 add_field / calculate_fields，"
            "要原地修复几何请用 repair_geometry。"
        )

    created = None if spec.get("output_is_container") else ensure_output_container(out)
    if path_exists(out):
        raise WorkerError('暂存输出意外存在，拒绝覆盖：' + out)

    started = time.time()
    extra: Dict[str, Any] = {}
    try:
        returned = spec["run"](in_paths, out, params)
        if isinstance(returned, dict):
            extra = returned
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError(
            f"{spec['summary']}失败：{exc}\n"
            f"输入：{in_paths}\n输出：{out}\n参数：{json.dumps(params, ensure_ascii=False, default=str)}"
            f"{where_hint(in_paths, params)}"
        ) from exc

    result: Dict[str, Any] = {
        "operation": operation,
        "operation_summary": spec["summary"],
        "output": out,
        "inputs": in_paths,
        "params": jsonable(params),
        "elapsed_seconds": round(time.time() - started, 2),
    }
    if created:
        result["container_note"] = created
    result.update(extra)

    # 输出必须真的生成了 —— 有些工具（例如 FeaturesToJSON）会「返回成功但没写文件」，
    # 或者按自己的规则改后缀（GeoJSON 一定写成 .geojson）。这里以操作报回来的
    # 真实路径为准，既兜住「悄悄没写」，也不让调用方去读错路径。
    actual_out = str(extra.get("output") or out)
    if not (path_exists(actual_out) or os.path.exists(actual_out)):
        raise WorkerError(
            f"{spec['summary']}的返回是成功的，但没有生成输出文件：{actual_out}\n"
            "可能是参数组合让 arcpy 换了输出位置/名字，或者输入数据为空。"
        )
    try:
        result.update(describe_dataset(actual_out))
    except Exception as exc:
        # 输出不是 arcpy 能描述的数据集（.json / .geojson / .xlsx 这类），只报文件信息
        result["describe_skipped"] = (
            f"{type(exc).__name__}：这个输出不是 arcpy 数据集，只回了文件信息"
        )
        with contextlib.suppress(OSError):
            result["file_size_bytes"] = os.path.getsize(actual_out)
    result["output"] = actual_out
    return result


def _op_edit(operation: str, target: str,
            params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """跑一个**原地修改**的操作（字段编辑、修复几何等）。"""
    import arcpy

    arcpy.env.overwriteOutput = False

    spec = EDITING.get(str(operation))
    if spec is None:
        raise WorkerError(f"不支持的编辑操作 {operation!r}，可用：{sorted(EDITING)}")
    params = dict(params or {})
    unknown = set(params) - set(spec["allowed"])
    if unknown:
        raise WorkerError(f"{operation} 不认识这些参数：{sorted(unknown)}；"
                          f"可用参数：{sorted(spec['allowed']) or '（无）'}")
    path = str(target or "").strip()
    if not path_exists(path):
        raise WorkerError(f"要修改的数据集不存在或打不开：{path}")
    try:
        result = spec["run"](path, params) or {}
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError(
            f"{spec['summary']}失败：{exc}\n"
            f"数据集：{path}\n参数：{json.dumps(params, ensure_ascii=False, default=str)}"
            f"{where_hint([path], params)}"
        ) from exc
    result.setdefault("in_features", path)
    result["operation"] = operation
    result["operation_summary"] = spec["summary"]
    result["modified_in_place"] = True
    return result


def op_geoprocess(operation: str, inputs=None, output=None, params=None, overwrite=False):
    """Publish new immutable results only after successful processing."""
    import arcpy
    out = str(output or '').strip()
    if not out:
        raise WorkerError('必须指定输出路径')
    if operation == 'features_to_json':
        out = str(Path(out).with_suffix('.geojson' if (params or {}).get('geojson') else '.json'))
    if any(path_key(out) == path_key(p) for p in (inputs or [])):
        raise WorkerError('输出和输入指向同一个数据集；请使用新的输出路径。')
    with safety.WorkspaceLease(out):
        publication = safety.Publication(out, overwrite, safety.CURRENT_ROOT)
        with arcpy.EnvManager(overwriteOutput=False):
            result = _op_geoprocess(operation, inputs, publication.work, params, False)
            actual = result.get('output', publication.work)
            final = publication.publish(actual)
        result['output'] = final
        result['path'] = final
        result['name'] = Path(final).name
        result['catalog_path'] = final
        result['requested_output'] = output
        result['retained_inputs'] = True
        result.update(publication.fields())
        return result


def op_edit(operation: str, target: str, params=None):
    """All edits create a retained version. The supplied source is never modified."""
    import arcpy
    if operation not in EDITING:
        raise WorkerError('不支持的编辑操作：' + operation)
    if not path_exists(target):
        raise WorkerError('输入不存在：' + target)
    if operation == 'delete_features' and not (params or {}).get('confirm'):
        raise WorkerError('删除记录需要 confirm=true；仅在副本中删除。')
    if operation == 'delete_dataset':
        if not (params or {}).get('confirm'):
            raise WorkerError('删除需 confirm=true；会将原件改名归档，使原路径消失，历史数据保留。')
        with safety.WorkspaceLease(target):
            publication = safety.Publication(target, overwrite=True, root=safety.CURRENT_ROOT)
            archived = publication.retire()
        return {'operation': operation, 'operation_summary': '归档删除数据集（原路径移除）',
                'source': target, 'in_features': target, 'output': archived, 'inputs': [archived],
                'params': params, 'logical_deleted': True, 'original_path_exists': False,
                'source_modified': True, 'modified_in_place': False, 'retained_inputs': True,
                **publication.fields(), 'target_replaced': False}
    with safety.WorkspaceLease(target):
        final = safety.version_path(target)
        publication = safety.Publication(final, root=safety.CURRENT_ROOT)
        with arcpy.EnvManager(overwriteOutput=False):
            safety.copy_for_edit(target, publication.work)
            result = _op_edit(operation, publication.work, params)
            final = publication.publish()
        # Do not leak internal work paths into logical history or tool responses.
        result['in_features'] = target
        result['output'] = final
        result['source'] = target
        result['inputs'] = [target] + list((params or {}).get('in_features') or [])
        result['params'] = params or {}
        result['modified_in_place'] = False
        result['source_modified'] = False
        result['operation_summary'] = '复制后' + spec_summary(operation)
        result['retained_inputs'] = True
        return result


def spec_summary(operation):
    return EDITING[operation]['summary'].split('（')[0]


def op_list_operations() -> Dict[str, Any]:
    """列出所有可用操作（给自检 / 文档用）。"""
    return {
        "geoprocessing": {k: v["summary"] for k, v in sorted(GEOPROCESSING.items())},
        "editing": {k: v["summary"] for k, v in sorted(EDITING.items())},
    }


def op_run_arcpy_code(code: str, project_root: Optional[str] = None,
                      timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    """在 arcpy 环境里执行一段 Python 代码。

    只有配置里开了 allow_arcpy_code，服务端才会注册对应的工具；这里实现保持不变，
    方便开启后直接用。代码里已经 import 好 arcpy，print 的内容会被收集返回，
    `result` 变量会被序列化返回。
    """
    import io
    import time

    import arcpy

    if not isinstance(code, str) or not code.strip():
        raise WorkerError("code 不能为空")

    stdout = io.StringIO()
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    namespace: Dict[str, Any] = {
        "arcpy": arcpy,
        "os": os,
        "sys": sys,
        "json": json,
        "Path": Path,
        "PROJECT_ROOT": root,          # 服务端当前认定的项目根目录，代码里直接用
        "arcpy_env": arcpy.env,
    }
    started = time.time()
    try:
        with contextlib.redirect_stdout(stdout):
            exec(compile(code, "<arcpy_code>", "exec"), namespace)
    except Exception as exc:
        raise WorkerError(
            f"代码执行出错：{type(exc).__name__}: {exc}\n"
            f"--- 执行时的 stdout ---\n{stdout.getvalue()[-2000:]}"
        ) from exc

    output = stdout.getvalue()
    return {
        "stdout": output[-8000:],
        "stdout_truncated": len(output) > 8000,
        "result": jsonable(namespace.get("result")),
        "elapsed_seconds": round(time.time() - started, 2),
        "project_root": str(root),
        "note": "这是在常驻 arcpy 进程里执行的。命名空间里已备好：arcpy、os、sys、json、"
                "Path、PROJECT_ROOT（当前项目根目录）、arcpy_env；print 的内容会被收集，"
                "result 变量会被返回。",
    }


OPS = {
    "ping": op_ping,
    "list_datasets": op_list_datasets,
    "describe": op_describe,
    "read": op_read,
    "geoprocess": op_geoprocess,
    "edit": op_edit,
    "list_operations": op_list_operations,
    **META,   # 通用层：list_arcpy_tools / describe_arcpy_tool / run_arcpy_tool
    "run_arcpy_code": op_run_arcpy_code,   # 只按配置注册的工具会用
}


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        PROTOCOL_STDOUT.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue  # 忽略杂音

        req_id = request.get("id")
        op = request.get("op")
        args = request.get("args") or {}
        audit = args.pop('_audit', None)
        handler = OPS.get(op)
        if handler is None:
            send({"id": req_id, "ok": False,
                  "error": {"type": "UnknownOp",
                            "message": f"不支持的操作：{op!r}，可用：{sorted(OPS)}"}})
            continue

        try:
            # arcpy 会往 stdout 打东西，统一改道到 stderr，保证协议通道干净
            with contextlib.redirect_stdout(sys.stderr):
                safety.CURRENT_ROOT = audit['root'] if audit else None
                safety.CURRENT_JOB = audit['job_id'] if audit else None
                if audit:
                    history = History(audit['root'])
                    safety.recover(audit['root'])
                    history.update(audit['job_id'], 'running')
                import arcpy
                # Arbitrary prior environments must not silently clip/filter later jobs.
                arcpy.ResetEnvironments()
                # Keep workspace locks through the logical audit commit, including path relocation.
                safety.DEFER_LEASE_RELEASE = bool(audit)
                result = handler(**args)
                result.setdefault('data_policy_version', DATA_POLICY_VERSION)
                if audit:
                    inputs = result.get('inputs', [])
                    outputs = ([result['output']] if result.get('output') else
                               [x['path'] for x in result.get('outputs', []) if x.get('exists')])
                    history.update(audit['job_id'], 'succeeded', result=result,
                                   inputs=inputs, outputs=outputs)
        except WorkerError as exc:
            exc = safety.lock_error(exc)
            if audit:
                with contextlib.suppress(Exception):
                    History(audit['root']).update(audit['job_id'], 'failed', error=str(exc))
            send({"id": req_id, "ok": False,
                  "error": {"type": "WorkerError", "message": str(exc)}})
        except Exception as exc:
            exc = safety.lock_error(exc)
            if audit:
                with contextlib.suppress(Exception):
                    History(audit['root']).update(audit['job_id'], 'unknown', error=str(exc))
            send({"id": req_id, "ok": False,
                  "error": {"type": type(exc).__name__, "message": str(exc),
                            "traceback": traceback.format_exc(limit=8)}})
        else:
            send({"id": req_id, "ok": True, "result": result})
        finally:
            safety.release_leases()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
