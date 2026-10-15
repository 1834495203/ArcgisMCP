"""数据管理操作：复制 / 合并 / 追加 / 投影 / 修复几何 / 创建 / 删除 / 几何与要素转换。

对应 ArcGIS 的 Data Management Tools。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import WorkerError, field_names, path_exists, unique_name

OPERATIONS: Dict[str, Dict[str, Any]] = {}


def register(name: str, summary: str, run, min_inputs: int = 1, allowed=(),
             output_is_container: bool = False) -> None:
    """output_is_container=True：输出本身就是「容器」（例如 create_gdb 输出一个 .gdb），
    这种操作要跳过「输出不能是 gdb 本身」的检查。"""
    OPERATIONS[name] = {"summary": summary, "run": run, "min_inputs": min_inputs,
                        "allowed": set(allowed), "output_is_container": output_is_container}


# --------------------------------------------------------------------------
def _copy_features(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """复制数据集（也可以用来 shp ↔ gdb 互转）。"""
    import arcpy

    arcpy.management.CopyFeatures(inputs[0], out)


def _merge(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把多个同类型的要素类上下拼成一个。"""
    import arcpy

    field_mappings = params.get("field_mappings")
    if field_mappings:
        mapping = arcpy.FieldMappings()
        mapping.loadFromString(str(field_mappings))
        arcpy.management.Merge(inputs, out, mapping, params.get("add_source"))
    else:
        arcpy.management.Merge(inputs, out, None, bool(params.get("add_source", False)))


def _project(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """投影转换（需要给目标坐标系）。"""
    import arcpy

    sr = params.get("spatial_reference")
    if sr in (None, ""):
        raise WorkerError("投影转换需要 spatial_reference：可以是 WKID（例如 4326、3857、"
                          "4547）、'WGS 1984 UTM Zone 50N' 这样的名字，或者 WKT 字符串")
    arcpy.management.Project(inputs[0], out, _make_sr(sr), params.get("transform_method"),
                             params.get("in_coordinate_system"))


def _define_projection(inputs: List[str], out: str,
                       params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """定义投影（数据本身坐标不变，只是声明它是什么坐标系）。"""
    import arcpy

    sr = params.get("spatial_reference")
    if sr in (None, ""):
        raise WorkerError("定义投影需要 spatial_reference")
    arcpy.management.CopyFeatures(inputs[0], out)
    arcpy.management.DefineProjection(out, _make_sr(sr))


def _make_sr(value: Any):
    import arcpy

    if isinstance(value, (int, float)):
        return arcpy.SpatialReference(int(value))
    text = str(value).strip()
    if text.isdigit():
        return arcpy.SpatialReference(int(text))
    if text.lstrip("-").replace(".", "").isdigit():
        return arcpy.SpatialReference(int(float(text)))
    return arcpy.SpatialReference(text)


def _check_geometry(inputs: List[str], out: str,
                    params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """几何检查：把有问题的要素 ID 和原因输出成一张表。"""
    import arcpy

    validation = str(params.get("validation_method") or "ESRI").upper()
    arcpy.management.CheckGeometry(inputs[0], out, validation)


def _create_gdb(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """创建文件地理数据库（out 写成 .../名字.gdb）。"""
    import arcpy

    if not str(out).lower().endswith(".gdb"):
        raise WorkerError(f"out_features 要以 .gdb 结尾，例如 ./out/新库.gdb（收到 {out}）")
    parent = str(Path(out).parent)
    name = Path(out).stem
    Path(parent).mkdir(parents=True, exist_ok=True)
    if path_exists(out):
        raise WorkerError(f"已存在：{out}")
    version = str(params.get("version") or "CURRENT").upper()
    arcpy.management.CreateFileGDB(parent, name, version)


def _create_feature_class(inputs: List[str], out: str,
                          params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """创建空要素类 / shapefile（按模板或指定几何类型）。"""
    import arcpy

    template = inputs[0] if inputs else None
    geometry_type = str(params.get("geometry_type") or "").upper() or None
    if geometry_type is None and not template:
        raise WorkerError("创建要素类需要 geometry_type（POINT / MULTIPOINT / POLYLINE / "
                          "POLYGON / MULTIPATCH）或者给一个模板数据集（in_features）")
    parent = str(Path(out).parent)
    name = Path(out).name
    Path(parent).mkdir(parents=True, exist_ok=True)
    sr = _make_sr(params["spatial_reference"]) if params.get("spatial_reference") else None
    arcpy.management.CreateFeatureclass(parent, name, geometry_type, template,
                                        "DISABLED", "DISABLED", sr)


def _create_table(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """创建空表。"""
    import arcpy

    template = inputs[0] if inputs else None
    parent = str(Path(out).parent)
    name = Path(out).name
    Path(parent).mkdir(parents=True, exist_ok=True)
    arcpy.management.CreateTable(parent, name, template)


def _feature_to_point(inputs: List[str], out: str,
                      params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """要素转点（面的内部点 / 线的端点等）。"""
    import arcpy

    location = str(params.get("point_location") or "CENTROID").upper()
    if location not in ("CENTROID", "INSIDE"):
        raise WorkerError(f"point_location 只能是 CENTROID（几何重心，可能落在面外）/ "
                          f"INSIDE（保证落在面内），收到 {location!r}")
    arcpy.management.FeatureToPoint(inputs[0], out, location)


def _feature_to_line(inputs: List[str], out: str,
                     params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """要素转线（面边界转线；多个输入时会把公共边处理掉）。"""
    import arcpy

    attributes = str(params.get("attributes") or "ATTRIBUTES").upper()
    if attributes not in ("ATTRIBUTES", "NO_ATTRIBUTES"):
        raise WorkerError(f"attributes 只能是 ATTRIBUTES / NO_ATTRIBUTES，收到 {attributes!r}")
    arcpy.management.FeatureToLine(inputs, out, params.get("cluster_tolerance"), attributes)


def _minimum_bounding_geometry(inputs: List[str], out: str,
                               params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """最小包围几何（矩形 / 凸包 / 圆 / 包络）。"""
    import arcpy

    geometry_type = str(params.get("geometry_type") or "RECTANGLE_BY_AREA").upper()
    allowed = ("RECTANGLE_BY_AREA", "RECTANGLE_BY_WIDTH", "CONVEX_HULL", "CIRCLE",
               "ENVELOPE", "RECTANGLE_BY_AREA_GEODESIC", "RECTANGLE_BY_WIDTH_GEODESIC",
               "CONVEX_HULL_GEODESIC", "CIRCLE_GEODESIC", "ENVELOPE_GEODESIC")
    if geometry_type not in allowed:
        raise WorkerError(f"geometry_type 不认识：{geometry_type!r}\n可用：{'、'.join(allowed)}")
    group_option = str(params.get("group_option") or "NONE").upper()
    if group_option not in ("NONE", "ALL", "LIST"):
        raise WorkerError(f"group_option 只能是 NONE / ALL / LIST，收到 {group_option!r}")
    arcpy.management.MinimumBoundingGeometry(
        inputs[0], out, geometry_type, group_option, params.get("group_field"),
        bool(params.get("add_geometry", False)))


def _sort(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按字段排序输出。

    实测：把 "NAME ASC" 整串传进去，arcpy 会当成字段名去找，报「表中不存在字段 NAME ASC」；
    正确写法是 [["NAME", "ASC"], ["AREA", "DESC"]]，所以这里做一次解析。
    """
    import arcpy

    fields = params.get("sort_field")
    if not fields:
        raise WorkerError('排序需要 sort_field，例如 ["POP DESC", "NAME ASC"]')
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.replace(";", ",").split(",") if f.strip()]

    parsed: List[List[str]] = []
    for item in fields:
        if isinstance(item, (list, tuple)):
            if len(item) != 2:
                raise WorkerError(f"排序项 {item!r} 应该是 [字段, 方向] 两个元素")
            field, direction = str(item[0]).strip(), str(item[1]).strip().upper()
        else:
            parts = str(item).replace(",", " ").split()
            if len(parts) == 1:
                field, direction = parts[0], "ASC"
            elif len(parts) == 2:
                field, direction = parts[0], parts[1].upper()
            else:
                raise WorkerError(f'排序项 {item!r} 要写成「字段 方向」，例如 "POP DESC"')
        # 实测：arcpy 的方向枚举是 ASCENDING / DESCENDING，不是 ASC / DESC
        direction = {"ASC": "ASCENDING", "DESCENDING": "DESCENDING",
                     "DESC": "DESCENDING", "ASCENDING": "ASCENDING"}.get(direction, direction)
        if direction not in ("ASCENDING", "DESCENDING"):
            raise WorkerError(f"排序方向 {direction!r} 只能是 ASC（升序）或 DESC（降序）")
        parsed.append([field, direction])
    arcpy.management.Sort(inputs[0], out, parsed, params.get("spatial_sort_method"))


def _multipart_to_singlepart(inputs: List[str], out: str,
                             params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """多部件转单部件。"""
    import arcpy

    arcpy.management.MultipartToSinglepart(inputs[0], out)


def _dissolve(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """融合。"""
    import arcpy

    fields = params.get("dissolve_field")
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.replace(";", ",").split(",") if f.strip()]
    stats = params.get("statistics_fields")
    if isinstance(stats, str):
        stats = [s.strip() for s in stats.replace(";", ",").split(",") if s.strip()]
    multi_part = "MULTI_PART" if params.get("multi_part", True) else "SINGLE_PART"
    arcpy.management.Dissolve(inputs[0], out, fields or None, stats or None,
                              multi_part, "UNSPLIT_LINES")


def _eliminate(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """消除：把符合条件的小面并进邻面。

    实测：Eliminate 的 in_features 必须是「带选择的图层」，直接传要素类路径、
    或把 where 当 ex_where_clause 传，都会失败。
    """
    import arcpy

    where = (params.get("where") or "").strip()
    if not where:
        raise WorkerError('消除必须给出 where：说明要消除哪些面。例如 where="Shape_Area < 0.1"')
    merge_with = str(params.get("merge_with") or "LENGTH").upper()
    if merge_with not in ("LENGTH", "AREA"):
        raise WorkerError(f"merge_with 只能是 LENGTH（并入共享边界最长的邻面）"
                          f"或 AREA（并入面积最大的邻面），收到 {merge_with!r}")

    layer = unique_name("arcgis_mcp_eliminate")
    try:
        arcpy.management.MakeFeatureLayer(inputs[0], layer)
        arcpy.management.SelectLayerByAttribute(layer, "NEW_SELECTION", where)
        selected = int(arcpy.management.GetCount(layer)[0])
        if selected == 0:
            # 没有要消除的要素是正常情况（例如「消除两次」的第二遍），给一份原样副本，
            # 让这一步可以安全重复执行，而不是让整条流程断掉。
            arcpy.management.CopyFeatures(inputs[0], out)
            return {
                "eliminated_count": 0,
                'selected_count': 0, 'geometry_attributes_recalculated': False,
                'area_field_warning': 'AREA_MU 等普通面积属性不会随几何自动更新；下次按面积筛选前请确认已重算。',
                "note": (f"where（{where}）没有选中任何要素，没有需要消除的图斑；"
                         "输出是输入的原样副本，流程可以继续。"),
            }
        arcpy.management.Eliminate(layer, out, merge_with)
        before = int(arcpy.management.GetCount(inputs[0])[0])
        after = int(arcpy.management.GetCount(out)[0])
        return {"eliminated_count": before - after, 'selected_count': selected,
                'unmerged_selected_count': selected - (before - after), "merge_with": merge_with,
                'geometry_attributes_recalculated': False,
                'area_field_warning': '消除改变几何，但 AREA_MU 等普通属性字段不会随几何重算。'
                    '下一遍若按该字段筛选，请先对返回 output 重新计算面积；不要把继承的旧面积当成真实面积。'}
    finally:
        with contextlib.suppress(Exception):
            arcpy.management.Delete(layer)


def _update(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """更新：用 update_features 更新 in_features，输出到新数据集（不改原数据）。"""
    import arcpy

    keep_borders = str(params.get("keep_borders") or "BORDERS").upper()
    if keep_borders not in ("BORDERS", "NO_BORDERS"):
        raise WorkerError(f"keep_borders 只能是 BORDERS / NO_BORDERS，收到 {keep_borders!r}")
    arcpy.analysis.Update(inputs[0], inputs[1], out, keep_borders,
                          params.get("cluster_tolerance"))


# --------------------------------------------------------------------------
register("copy_features", "复制数据集（也可用于 shp ↔ gdb 转换）", _copy_features)
register("merge", "合并多个数据集（上下拼接）", _merge, min_inputs=2,
         allowed={"field_mappings", "add_source"})
register("project", "投影转换", _project, min_inputs=1,
         allowed={"spatial_reference", "transform_method", "in_coordinate_system"})
register("define_projection", "定义投影（只声明坐标系，不改坐标）", _define_projection,
         min_inputs=1, allowed={"spatial_reference"})
register("check_geometry", "几何检查（输出问题清单表）", _check_geometry, min_inputs=1,
         allowed={"validation_method"})
register("create_gdb", "创建文件地理数据库", _create_gdb, min_inputs=0,
         allowed={"version"}, output_is_container=True)
register("create_feature_class", "创建空要素类 / shapefile", _create_feature_class,
         min_inputs=0, allowed={"geometry_type", "spatial_reference"})
register("create_table", "创建空表", _create_table, min_inputs=0)
register("feature_to_point", "要素转点", _feature_to_point, min_inputs=1, allowed={"point_location"})
register("feature_to_line", "要素转线（面边界转线）", _feature_to_line, min_inputs=1,
         allowed={"cluster_tolerance", "attributes"})
register("minimum_bounding_geometry", "最小包围几何（矩形/凸包/圆）", _minimum_bounding_geometry,
         min_inputs=1, allowed={"geometry_type", "group_option", "group_field", "add_geometry"})
register("sort", "按字段排序", _sort, min_inputs=1,
         allowed={"sort_field", "spatial_sort_method"})
register("multipart_to_singlepart", "多部件转单部件", _multipart_to_singlepart)
register("dissolve", "融合", _dissolve, min_inputs=1,
         allowed={"dissolve_field", "statistics_fields", "multi_part"})
register("eliminate", "消除（把小面并进邻面）", _eliminate, min_inputs=1,
         allowed={"where", "merge_with"})
register("update", "更新（用一份面更新另一份面，输出新数据集）", _update, min_inputs=2,
         allowed={"keep_borders", "cluster_tolerance"})
