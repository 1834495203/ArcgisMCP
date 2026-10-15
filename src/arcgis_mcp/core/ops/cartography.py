"""制图综合操作：平滑 / 抽稀（简化）/ 加密顶点。

对应 ArcGIS 的 Cartography Tools。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .common import WorkerError

OPERATIONS: Dict[str, Dict[str, Any]] = {}


def register(name: str, summary: str, run, min_inputs: int = 1, allowed=()) -> None:
    OPERATIONS[name] = {"summary": summary, "run": run,
                        "min_inputs": min_inputs, "allowed": set(allowed)}


def _shape_type(path: str) -> str:
    import arcpy

    return str(getattr(arcpy.Describe(path), "shapeType", "") or "").lower()


def _smooth(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """平滑线 / 面的尖锐拐角（按几何类型自动选 SmoothLine / SmoothPolygon）。"""
    import arcpy

    algorithm = str(params.get("algorithm") or "PAEK").upper()
    if algorithm not in ("PAEK", "BEZIER_INTERPOLATION"):
        raise WorkerError(f"algorithm 只能是 PAEK 或 BEZIER_INTERPOLATION，收到 {algorithm!r}")
    tolerance = params.get("tolerance")
    if tolerance is None:
        raise WorkerError("平滑需要 tolerance（容差），单位与数据一致")
    tolerance = float(tolerance)
    if tolerance <= 0:
        raise WorkerError("tolerance 必须大于 0")

    shape = _shape_type(inputs[0])
    if shape == "polygon":
        fn = arcpy.cartography.SmoothPolygon
    elif shape == "polyline":
        fn = arcpy.cartography.SmoothLine
    else:
        raise WorkerError(f"平滑只支持线或面，输入是 {shape or '未知类型'}")
    # 实测：这个版本只吃前 4 个参数，多传 endpoint_option/error_option 会「参数无效」
    fn(inputs[0], out, algorithm, tolerance)


def _simplify(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """抽稀（简化）线 / 面的顶点（按几何类型自动选 SimplifyLine / SimplifyPolygon）。

    和「平滑」不同：抽稀是去掉多余顶点、保留整体形状；平滑是把尖锐拐角变圆润。
    """
    import arcpy

    algorithm = str(params.get("algorithm") or "POINT_REMOVE").upper()
    allowed = ("POINT_REMOVE", "BEND_SIMPLIFY", "WEIGHTED_AREA", "EFFECTIVE_AREA")
    if algorithm not in allowed:
        raise WorkerError(f"algorithm 不认识：{algorithm!r}\n可用：{'、'.join(allowed)}\n"
                          "其中 POINT_REMOVE 最常用；BEND_SIMPLIFY 只适用于线")
    tolerance = params.get("tolerance")
    if tolerance is None:
        raise WorkerError("抽稀需要 tolerance（容差），单位与数据一致；值越大越简略")
    tolerance = float(tolerance)
    if tolerance <= 0:
        raise WorkerError("tolerance 必须大于 0")

    shape = _shape_type(inputs[0])
    if shape == "polygon":
        if algorithm == "BEND_SIMPLIFY":
            raise WorkerError("BEND_SIMPLIFY 算法只支持线，面请用 POINT_REMOVE / WEIGHTED_AREA")
        fn = arcpy.cartography.SimplifyPolygon
    elif shape == "polyline":
        fn = arcpy.cartography.SimplifyLine
    else:
        raise WorkerError(f"抽稀只支持线或面，输入是 {shape or '未知类型'}")
    # 实测：这个参数是 RESOLVE_ERRORS / FLAG_ERRORS，不是 NO_CHECK
    fn(inputs[0], out, algorithm, tolerance, "RESOLVE_ERRORS")


def _densify(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """加密顶点（在顶点之间插入更多点）。

    实测：arcpy.edit.Densify 是原地修改输入，所以这里先复制一份再在副本上跑，
    保持「输出是新数据集、不动原数据」的约定。
    """
    import arcpy

    method = str(params.get("densification_method") or "DISTANCE").upper()
    if method not in ("DISTANCE", "ANGLE", "MAX_DEVIATION"):
        raise WorkerError(f"densification_method 只能是 DISTANCE / ANGLE / MAX_DEVIATION，"
                          f"收到 {method!r}")
    arcpy.management.CopyFeatures(inputs[0], out)
    if method == "DISTANCE":
        distance = params.get("distance")
        if distance in (None, ""):
            raise WorkerError("densification_method=DISTANCE 时必须给 distance（最大顶点间距）")
        arcpy.edit.Densify(out, method, distance)
    elif method == "ANGLE":
        max_angle = params.get("max_angle")
        if max_angle in (None, ""):
            raise WorkerError("densification_method=ANGLE 时必须给 max_angle（最大角度，单位度）")
        arcpy.edit.Densify(out, method, None, None, max_angle)
    else:
        max_deviation = params.get("max_deviation")
        if max_deviation in (None, ""):
            raise WorkerError("densification_method=MAX_DEVIATION 时必须给 max_deviation")
        arcpy.edit.Densify(out, method, None, max_deviation)


register("smooth", "平滑线/面的尖锐拐角", _smooth, min_inputs=1,
         allowed={"algorithm", "tolerance"})
register("simplify", "抽稀（简化）顶点", _simplify, min_inputs=1,
         allowed={"algorithm", "tolerance"})
register("densify", "加密顶点", _densify, min_inputs=1,
         allowed={"densification_method", "distance", "max_deviation", "max_angle"})
