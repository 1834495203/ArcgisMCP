"""矢量分析操作：叠加分析 + 邻域分析。

对应 ArcGIS 的 Analysis Tools / Overlay & Proximity 家族。
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import WorkerError, delete_dataset, unique_name

OPERATIONS: Dict[str, Dict[str, Any]] = {}


def register(name: str, summary: str, run, min_inputs: int = 1, allowed=()) -> None:
    OPERATIONS[name] = {"summary": summary, "run": run,
                        "min_inputs": min_inputs, "allowed": set(allowed)}


# --------------------------------------------------------------------------
# 叠加分析
# --------------------------------------------------------------------------
def _join_attributes(params: Dict[str, Any]) -> str:
    value = str(params.get("join_attributes") or "ALL").upper()
    if value not in ("ALL", "NO_FID", "ONLY_FID"):
        raise WorkerError(f"join_attributes 只能是 ALL / NO_FID / ONLY_FID，收到 {value!r}")
    return value


def _output_type(params: Dict[str, Any]) -> str:
    value = str(params.get("output_type") or "INPUT").upper()
    if value not in ("INPUT", "LINE", "POINT"):
        raise WorkerError(f"output_type 只能是 INPUT / LINE / POINT，收到 {value!r}")
    return value


def _intersect(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    arcpy.analysis.Intersect(inputs, out, _join_attributes(params),
                             params.get("cluster_tolerance"), _output_type(params))


def _union(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    gaps = str(params.get("gaps") or "GAPS").upper()
    if gaps not in ("GAPS", "NO_GAPS"):
        raise WorkerError(f"gaps 只能是 GAPS / NO_GAPS，收到 {gaps!r}")
    arcpy.analysis.Union(inputs, out, _join_attributes(params),
                         params.get("cluster_tolerance"), gaps)


def _erase(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    arcpy.analysis.Erase(inputs[0], inputs[1], out, params.get("cluster_tolerance"))


def _clip(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    arcpy.analysis.Clip(inputs[0], inputs[1], out, params.get("cluster_tolerance"))


def _identity(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    relationship = str(params.get("relationship") or "NO_RELATIONSHIPS").upper()
    if relationship not in ("NO_RELATIONSHIPS", "KEEP_RELATIONSHIPS"):
        raise WorkerError(f"relationship 只能是 NO_RELATIONSHIPS / KEEP_RELATIONSHIPS，"
                          f"收到 {relationship!r}")
    arcpy.analysis.Identity(inputs[0], inputs[1], out, _join_attributes(params),
                            params.get("cluster_tolerance"), relationship)


def _symmetrical_difference(inputs: List[str], out: str,
                            params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """对称差：两个图层互不重叠的部分。

    arcpy 3.1 里没有 SymmetricalDifference（那是 ArcMap 时代的工具，Pro 已移除），
    所以这里用「A 擦除 B」+「B 擦除 A」再合并的组合实现。
    """
    import arcpy

    workdir = Path(out).parent / unique_name("symdiff_tmp")
    workdir.mkdir(parents=True, exist_ok=True)
    only_a = str(workdir / "only_a.shp")
    only_b = str(workdir / "only_b.shp")
    try:
        arcpy.analysis.Erase(inputs[0], inputs[1], only_a, params.get("cluster_tolerance"))
        arcpy.analysis.Erase(inputs[1], inputs[0], only_b, params.get("cluster_tolerance"))
        arcpy.management.Merge([only_a, only_b], out)
    finally:
        delete_dataset(only_a)
        delete_dataset(only_b)
        with contextlib.suppress(Exception):
            shutil.rmtree(workdir, ignore_errors=True)
    return {"implementation_note":
            "arcpy 3.1 没有对称差工具，这次是「A 擦除 B」+「B 擦除 A」再合并得到的"}


# --------------------------------------------------------------------------
# 邻域分析
# --------------------------------------------------------------------------
def _buffer(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    distance = params.get("distance")
    if distance in (None, ""):
        raise WorkerError("buffer 需要 distance（缓冲距离，单位与数据一致；"
                          "也可以给字段名让每个要素用不同距离）")
    dissolve_option = str(params.get("dissolve_option") or "NONE").upper()
    if dissolve_option not in ("NONE", "ALL", "LIST"):
        raise WorkerError(f"dissolve_option 只能是 NONE / ALL / LIST，收到 {dissolve_option!r}")
    dissolve_field = params.get("dissolve_field")
    if dissolve_option == "LIST" and not dissolve_field:
        raise WorkerError("dissolve_option=LIST 时必须给 dissolve_field（按哪些字段融合缓冲区）")
    arcpy.analysis.Buffer(inputs[0], out, distance, params.get("line_side"),
                          params.get("line_end_type"), dissolve_option,
                          dissolve_field, params.get("method"))


def _spatial_join(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    join_operation = str(params.get("join_operation") or "JOIN_ONE_TO_ONE").upper()
    if join_operation not in ("JOIN_ONE_TO_ONE", "JOIN_ONE_TO_MANY"):
        raise WorkerError(f"join_operation 只能是 JOIN_ONE_TO_ONE / JOIN_ONE_TO_MANY，"
                          f"收到 {join_operation!r}")
    join_type = str(params.get("join_type") or "KEEP_ALL").upper()
    if join_type not in ("KEEP_ALL", "KEEP_COMMON"):
        raise WorkerError(f"join_type 只能是 KEEP_ALL（保留全部目标要素）/ "
                          f"KEEP_COMMON（只保留匹配上的），收到 {join_type!r}")
    match_option = str(params.get("match_option") or "INTERSECT").upper()
    allowed_match = ("INTERSECT", "INTERSECT_3D", "WITHIN_A_DISTANCE", "WITHIN_A_DISTANCE_3D",
                     "WITHIN_A_DISTANCE_GEODESIC", "CONTAINS", "COMPLETELY_CONTAINS",
                     "COMPLETELY_WITHIN", "WITHIN", "CROSSED_BY_THE_OUTLINE_OF",
                     "HAVE_THEIR_CENTER_IN", "ARE_IDENTICAL_TO", "BOUNDARY_TOUCHES",
                     "SHARE_A_LINE_SEGMENT_WITH", "CLOSEST")
    if match_option not in allowed_match:
        raise WorkerError(f"match_option 不认识：{match_option!r}\n可用：{'、'.join(allowed_match)}")
    if match_option.startswith("WITHIN_A_DISTANCE") and params.get("search_radius") in (None, ""):
        raise WorkerError(f"match_option={match_option} 时必须给 search_radius")
    arcpy.analysis.SpatialJoin(inputs[0], inputs[1], out, join_operation, join_type,
                               None, match_option, params.get("search_radius"),
                               params.get("distance_field_name"))


def _near(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """近邻分析：结果是给输入要素加 NEAR_ 系列字段，所以输出是「带结果的副本」。

    arcpy 的 Near 会原地改输入，所以这里先复制一份再在副本上跑，保持「不动原始数据」的约定。
    """
    import arcpy

    arcpy.management.CopyFeatures(inputs[0], out)
    near_features = inputs[1:]
    location = str(params.get("location") or "NO_LOCATION").upper()
    angle = str(params.get("angle") or "NO_ANGLE").upper()
    method = str(params.get("method") or "PLANAR").upper()
    arcpy.analysis.Near(out, near_features, params.get("search_radius"), location, angle,
                        method, params.get("field_names"))

    # 把最近距离汇总出来，方便确认效果
    distances: List[float] = []
    with arcpy.da.SearchCursor(out, ["NEAR_DIST"]) as cursor:
        for (value,) in cursor:
            if value is not None:
                distances.append(float(value))
    return {
        "near_count": len(distances),
        "min_distance": min(distances) if distances else None,
        "max_distance": max(distances) if distances else None,
        "note": "结果数据集里多了 NEAR_FID / NEAR_DIST" +
                ("/ NEAR_X / NEAR_Y" if location == "LOCATION" else "") + " 等字段",
    }


def _select_by_location(inputs: List[str], out: str,
                        params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按空间关系筛选导出（SelectLayerByLocation + CopyFeatures）。"""
    import arcpy

    relation = str(params.get("relation") or "INTERSECT").upper()
    allowed_relation = ("INTERSECT", "INTERSECT_3D", "WITHIN_A_DISTANCE",
                        "WITHIN_A_DISTANCE_3D", "WITHIN_A_DISTANCE_GEODESIC", "CONTAINS",
                        "COMPLETELY_CONTAINS", "WITHIN", "COMPLETELY_WITHIN", "CROSSES",
                        "TOUCHES", "OVERLAPS", "SHARE_A_LINE_SEGMENT_WITH",
                        "BOUNDARY_TOUCHES", "HAVE_THEIR_CENTER_IN", "ARE_IDENTICAL_TO")
    if relation not in allowed_relation:
        raise WorkerError(f"relation 不认识：{relation!r}\n可用：{'、'.join(allowed_relation)}")
    if relation.startswith("WITHIN_A_DISTANCE") and params.get("search_distance") in (None, ""):
        raise WorkerError(f"relation={relation} 时必须给 search_distance")

    layer_name = unique_name("arcgis_mcp_sbl")
    try:
        arcpy.management.MakeFeatureLayer(inputs[0], layer_name)
        arcpy.management.SelectLayerByLocation(
            layer_name, relation, inputs[1], params.get("search_distance"),
            "NEW_SELECTION", bool(params.get("invert", False)))
        selected = int(arcpy.management.GetCount(layer_name)[0])
        if selected == 0:
            # 没有命中的要素：给一份空结果（而不是报错），方便流程继续
            arcpy.management.CopyFeatures(layer_name, out)
            return {"matched_count": 0,
                    "note": f"没有要素满足 {relation} 关系，输出是空数据集"}
        arcpy.management.CopyFeatures(layer_name, out)
        return {"matched_count": selected}
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            arcpy.management.Delete(layer_name)


def _select(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    where = (params.get("where") or "").strip()
    if not where:
        raise WorkerError("筛选导出必须给 where：说明要导出哪些要素。"
                          "例如查询土壤类型，where 写成 TYPE = '寒冻土'")
    arcpy.analysis.Select(inputs[0], out, where)


# --------------------------------------------------------------------------
register("intersect", "相交", _intersect, min_inputs=2,
         allowed={"join_attributes", "output_type", "cluster_tolerance"})
register("union", "联合（求并集）", _union, min_inputs=2,
         allowed={"join_attributes", "cluster_tolerance", "gaps"})
register("erase", "擦除（从 in_features 中挖掉 erase_features）", _erase, min_inputs=2,
         allowed={"cluster_tolerance"})
register("clip", "裁剪（按 clip_features 的范围裁剪）", _clip, min_inputs=2,
         allowed={"cluster_tolerance"})
register("identity", "标识（叠加并保留全部输入范围）", _identity, min_inputs=2,
         allowed={"join_attributes", "cluster_tolerance", "relationship"})
register("symmetrical_difference", "对称差（两边不重叠的部分）", _symmetrical_difference,
         min_inputs=2, allowed={"join_attributes", "cluster_tolerance"})
register("buffer", "缓冲区", _buffer, min_inputs=1,
         allowed={"distance", "line_side", "line_end_type", "dissolve_option",
                  "dissolve_field", "method"})
register("spatial_join", "空间连接（按空间关系把属性连过来）", _spatial_join, min_inputs=2,
         allowed={"join_operation", "join_type", "match_option", "search_radius",
                  "distance_field_name"})
register("near", "近邻分析（找最近的要素并算距离）", _near, min_inputs=2,
         allowed={"search_radius", "location", "angle", "method", "field_names"})
register("select_by_location", "按位置筛选导出", _select_by_location, min_inputs=2,
         allowed={"relation", "search_distance", "invert"})
register("select", "按属性筛选导出子集", _select, min_inputs=1, allowed={"where"})
