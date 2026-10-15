"""所有操作共用的东西：错误类型、值序列化、路径/字段/输出容器处理。

只依赖标准库和 arcpy（arcpy 在用到的函数里 lazy import），
以便被 ArcGIS Pro 的 Python 3.9 直接按文件路径加载。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

GDB_SUFFIXES = (".gdb", ".mdb")
SHP_SUFFIX = ".shp"

#: AddField / CalculateField 允许的字段类型
FIELD_TYPES = ("TEXT", "DOUBLE", "FLOAT", "SHORT", "LONG", "BIGINTEGER", "DATE", "GUID")

#: 几何属性（AddGeometryAttributes）支持的属性。实测确认：
#: 经纬度（Geographic）数据**不接受平面面积/长度**，只认 *_GEODESIC 版本，
#: 所以下面还给了平面→大地线的替代映射，由 attributes.py 自动替换。
GEOMETRY_PROPERTIES = (
    "AREA", "AREA_GEODESIC", "PERIMETER_LENGTH", "PERIMETER_LENGTH_GEODESIC",
    "LENGTH", "LENGTH_GEODESIC", "LENGTH3D", "LENGTH3D_GEODESIC",
    "CENTROID", "CENTROID_INSIDE", "LINE_START_MID_END", "PART_COUNT",
    "POINT_COUNT", "EXTENT",
)
#: 经纬度数据下，平面属性要换成大地线属性
PLANAR_TO_GEODESIC = {
    "AREA": "AREA_GEODESIC",
    "PERIMETER_LENGTH": "PERIMETER_LENGTH_GEODESIC",
    "LENGTH": "LENGTH_GEODESIC",
    "LENGTH3D": "LENGTH3D_GEODESIC",
}
#: 常用长度/面积单位（AddGeometryAttributes 用）
GEOMETRY_UNITS = ("METERS", "KILOMETERS", "FEET", "MILES", "NAUTICAL_MILES", "YARDS",
                  "SQUARE_METERS", "HECTARES", "SQUARE_KILOMETERS", "ACRES", "SQUARE_FEET",
                  "SQUARE_MILES", "SQUARE_YARDS", "SQUARE_NAUTICAL_MILES")


class WorkerError(RuntimeError):
    """可预期的业务错误，消息会原样返回给 AI。"""


# --------------------------------------------------------------------------
# 值序列化
# --------------------------------------------------------------------------
def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


def brief(value: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------
# 数据集路径 / 存在性
# --------------------------------------------------------------------------
def path_key(path: str) -> str:
    """路径比较用的规范化 key（判断「输出是不是就是输入本身」）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def path_exists(path: str) -> bool:
    """arcpy 版的「存在吗」——能识别 gdb 内部的要素类。"""
    import arcpy

    with contextlib.suppress(Exception):
        if arcpy.Exists(str(path)):
            return True
    return os.path.exists(str(path))


def delete_dataset(path: str) -> None:
    import arcpy

    if path_exists(path):
        arcpy.management.Delete(str(path))


def ensure_output_container(out: str) -> Optional[str]:
    """保证输出的「容器」存在：目录自动建，缺的 .gdb 自动创建。

    输出本身（要素类 / shapefile / 栅格）交给 arcpy 生成。
    """
    import arcpy

    path = Path(str(out))
    if path.suffix.lower() in GDB_SUFFIXES:
        raise WorkerError(
            f"输出要写成 gdb 内部的要素类名，不能是 gdb 本身：{out}\n"
            '例如 out_features="./out/结果.gdb/道路融合"'
        )
    parent = path.parent
    container = next((p for p in path.parents if p.suffix.lower() in GDB_SUFFIXES), None)
    if container is not None:
        from . import safety
        if path.suffix.lower() in ('.csv', '.dbf', '.json', '.geojson', '.xlsx', '.xls', '.shp', '.tif', '.img', '.txt', '.pdf'):
            raise WorkerError(f'文件格式输出必须位于 GDB 外的普通目录，不能写成库内文件：{out}')
        if container.suffix.lower() == '.mdb':
            raise WorkerError(f'个人地理数据库（.mdb）不支持新建：{container}')
        created = None
        if not path_exists(str(container)):
            if not safety.AUTO_CREATE_GDB:
                raise WorkerError(f'输出 GDB 不存在且 auto_create_gdb=false：{container}。'
                                  '请复用已有库，或显式调用 create_gdb 创建一次；不要每步新建库。')
            container.parent.mkdir(parents=True, exist_ok=True)
            arcpy.management.CreateFileGDB(str(container.parent), container.name)
            created = f'自动创建了地理数据库 {container}'
        if parent != container and not path_exists(str(parent)):
            raise WorkerError(f'GDB 中的要素数据集不存在，不会创建普通文件夹冒充：{parent}')
        return created
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        return f"自动创建了输出目录 {parent}"
    return None


def unique_name(prefix: str) -> str:
    return f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 100000}"


# --------------------------------------------------------------------------
# 字段 / 描述
# --------------------------------------------------------------------------
def field_names(path: str) -> List[str]:
    import arcpy

    return [f.name for f in arcpy.ListFields(str(path)) if f.type != "Geometry"]


def all_field_names(path: str) -> List[str]:
    import arcpy

    return [f.name for f in arcpy.ListFields(str(path))]


def validate_field_type(field_type: Any) -> str:
    value = str(field_type or "").strip().upper()
    if value not in FIELD_TYPES:
        raise WorkerError(f"field_type 只能是 {'/'.join(FIELD_TYPES)}，收到 {field_type!r}")
    return value


def count_where(path: str, where: str, is_table: bool = False) -> int:
    """按条件计数（建临时图层选中再数，比逐要素快）。"""
    import arcpy

    layer = unique_name("arcgis_mcp_count")
    try:
        if is_table:
            arcpy.management.MakeTableView(str(path), layer)
        else:
            arcpy.management.MakeFeatureLayer(str(path), layer)
        arcpy.management.SelectLayerByAttribute(layer, "NEW_SELECTION", where)
        return int(arcpy.management.GetCount(layer)[0])
    finally:
        with contextlib.suppress(Exception):
            arcpy.management.Delete(layer)


def ensure_dataset(path: str, label: str = "数据集") -> str:
    text = str(path or "").strip()
    if not text:
        raise WorkerError(f"{label}不能为空")
    if not path_exists(text):
        raise WorkerError(f"{label}不存在或打不开：{text}")
    return text


def where_hint(inputs: Sequence[str], params: Dict[str, Any]) -> str:
    """where 条件出错时，把可用字段列出来（这是最常见的错误）。"""
    if not any(k in params for k in ("where", "ex_where_clause", "expression")):
        return ""
    names: List[str] = []
    for path in inputs:
        with contextlib.suppress(Exception):
            names.extend(field_names(path))
    if not names:
        return ""
    hint = f"\n该数据集的字段有：{'、'.join(dict.fromkeys(names))}"
    hint += ("\n（注意：gdb 要素类常带 Shape_Area / Shape_Length，"
             "shapefile 通常没有面积字段，需要先用 add_field + calculate_fields 自己算）")
    return hint


def describe_dataset(path: str) -> Dict[str, Any]:
    """数据集摘要：类型、几何、要素数、坐标系、范围、字段。矢量栅格都能用。"""
    import arcpy

    desc = arcpy.Describe(str(path))
    info: Dict[str, Any] = {
        "path": str(path),
        "name": getattr(desc, "name", None),
        "data_type": desc.dataType,
        "catalog_path": getattr(desc, "catalogPath", None),
    }
    shape_type = getattr(desc, "shapeType", None)
    if shape_type:
        info["shape_type"] = shape_type
    sr = getattr(desc, "spatialReference", None)
    if sr is not None and getattr(sr, "name", None):
        info["spatial_reference"] = {
            "name": sr.name,
            "wkid": getattr(sr, "factoryCode", None),
            "type": getattr(sr, "type", None),
        }
    with contextlib.suppress(Exception):
        ext = desc.extent
        if ext:
            info["extent"] = {"xmin": ext.XMin, "ymin": ext.YMin, "xmax": ext.XMax, "ymax": ext.YMax}
    # 矢量 / 表：要素数 + 字段。两个名字都给：要素类叫 feature_count 更直观，
    # 表/统计结果叫 record_count 更准确，同一个值（避免调用方要记两套命名）。
    with contextlib.suppress(Exception):
        count = int(arcpy.management.GetCount(str(path))[0])
        info["record_count"] = count
        info["feature_count"] = count
    with contextlib.suppress(Exception):
        info["fields"] = [
            {"name": f.name, "type": f.type, "alias": f.aliasName,
             "length": f.length, "nullable": f.isNullable}
            for f in arcpy.ListFields(str(path)) if f.type != "Geometry"
        ]
    # 栅格专属信息
    if str(desc.dataType).lower() in ("rasterdataset", "raster", "rasterband", "gdb_rasterdataset"):
        with contextlib.suppress(Exception):
            info["band_count"] = int(getattr(desc, "bandCount", 0) or 0)
            info["pixel_type"] = getattr(desc, "pixelType", None)
            info["cell_size"] = {
                "x": getattr(getattr(desc, "meanCellWidth", None), "real", None),
                "y": getattr(getattr(desc, "meanCellHeight", None), "real", None),
            }
            info["compression_type"] = getattr(desc, "compressionType", None)
            info["raster_format"] = getattr(desc, "format", None)
    return info
