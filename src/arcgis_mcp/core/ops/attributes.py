"""字段与几何编辑算法；target 是外层安全流程复制出的工作副本。

算法修改传入的副本，公开工具通过 op_edit 保留源数据并发布新版本。
不要直接调用这些算法修改业务原件。扩展约定见根目录 AGENTS.md 和 docs/extending-tools.md。
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, List, Optional

from .common import (
    GEOMETRY_PROPERTIES,
    GEOMETRY_UNITS,
    PLANAR_TO_GEODESIC,
    WorkerError,
    count_where,
    field_names,
    unique_name,
    validate_field_type,
)

OPERATIONS: Dict[str, Dict[str, Any]] = {}


def register(name: str, summary: str, run, allowed=()) -> None:
    OPERATIONS[name] = {"summary": summary, "run": run, "allowed": set(allowed)}


# --------------------------------------------------------------------------
def _add_field(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import arcpy

    name = str(params.get("field") or "").strip()
    if not name:
        raise WorkerError("field（字段名）不能为空")
    ftype = validate_field_type(params.get("field_type"))
    existing = field_names(target)
    if any(n.lower() == name.lower() for n in existing):
        raise WorkerError(f"字段 {name!r} 已经存在了（现有字段：{'、'.join(existing)}）。"
                          "要改值请用 calculate_fields。")
    length = params.get("field_length")
    if ftype == "TEXT" and not length:
        length = 254  # 不给长度时 arcpy 默认 50，中文容易截断
    arcpy.management.AddField(target, name, ftype, field_length=length,
                              field_alias=params.get("field_alias"),
                              field_is_nullable=params.get("field_is_nullable", True))
    return {"in_features": target, "added_field": name, "field_type": ftype,
            "field_length": length, "fields": field_names(target)}


def _calculate_fields(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import arcpy

    assignments = params.get("assignments")
    if not isinstance(assignments, dict) or not assignments:
        raise WorkerError("assignments 不能为空，格式是 {字段名: 表达式}")
    expr_type = str(params.get("expression_type") or "PYTHON3").upper()
    if expr_type not in ("PYTHON3", "PYTHON", "SQL", "ARCADE"):
        raise WorkerError(f"expression_type 只能是 PYTHON3 / PYTHON / SQL / ARCADE，"
                          f"收到 {expr_type!r}")
    field_type = params.get("field_type")
    existing = {n.lower() for n in field_names(target)}
    created: List[str] = []
    for name in assignments:
        if str(name).lower() in existing:
            continue
        if not field_type:
            raise WorkerError(
                f"字段 {name!r} 不存在，也没给 field_type。\n"
                f"现有字段：{'、'.join(field_names(target))}\n"
                "要么先用 add_field 建字段，要么在 calculate_fields 里给 field_type 自动建。"
            )
        ftype = validate_field_type(field_type)
        arcpy.management.AddField(target, str(name), ftype,
                                  field_length=254 if ftype == "TEXT" else None)
        created.append(str(name))
        existing.add(str(name).lower())

    where = params.get("where")
    total = int(arcpy.management.GetCount(target)[0])
    matched = total
    view = None
    try:
        calculation_target = target
        if where:
            view = unique_name('mcp_calc')
            is_table = str(arcpy.Describe(target).dataType).lower() in ('table', 'dbasetable')
            make = arcpy.management.MakeTableView if is_table else arcpy.management.MakeFeatureLayer
            make(target, view, where)
            calculation_target = view
            matched = int(arcpy.management.GetCount(view)[0])
        for name, expression in assignments.items():
            if expression is None or str(expression) == '':
                raise WorkerError(f'字段 {name!r} 的表达式是空的，没法赋值')
            arcpy.management.CalculateField(calculation_target, str(name), str(expression), expr_type,
                                            params.get('code_block'), field_type)
    finally:
        if view:
            with contextlib.suppress(Exception):
                arcpy.management.Delete(view)
    return {
        "in_features": target,
        "updated_fields": list(assignments),
        "created_fields": created,
        "where": where,
        "feature_count": total,
        "updated_count": matched if where else total,
        "fields": field_names(target),
    }


def _delete_field(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import arcpy

    fields = params.get("field")
    if not fields:
        raise WorkerError('删字段需要 field，例如 field="TEMP" 或 field=["A", "B"]')
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.replace(";", ",").split(",") if f.strip()]
    existing = field_names(target)
    missing = [f for f in fields if f.lower() not in {n.lower() for n in existing}]
    if missing:
        raise WorkerError(f"字段不存在：{'、'.join(missing)}\n现有字段：{'、'.join(existing)}")
    arcpy.management.DeleteField(target, fields)
    return {"in_features": target, "deleted_fields": fields, "fields": field_names(target),
            "note": "字段已删除（不可恢复）"}


def _alter_field(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import arcpy

    field = params.get("field")
    if not field:
        raise WorkerError("改字段需要 field（要改的字段名）")
    kwargs: Dict[str, Any] = {}
    if params.get("new_name"):
        kwargs["new_field_name"] = params.get("new_name")
    if params.get("new_alias"):
        kwargs["new_field_alias"] = params.get("new_alias")
    if params.get("field_type"):
        kwargs["field_type"] = validate_field_type(params.get("field_type"))
    if params.get("field_length"):
        kwargs["field_length"] = int(params["field_length"])
    if not kwargs:
        raise WorkerError("至少要给 new_name（新字段名）/ new_alias（新别名）/ field_type "
                          "中的一项，否则没有东西可改")
    fields = {n.lower(): n for n in field_names(target)}
    if str(field).lower() not in fields:
        raise WorkerError(f"字段不存在：{field}\n现有字段：{'、'.join(fields.values())}")
    arcpy.management.AlterField(target, fields[str(field).lower()], **kwargs)
    return {"in_features": target, "field": field, "changed": kwargs,
            "fields": field_names(target)}


def _add_xy_coordinates(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """给点要素加上 POINT_X / POINT_Y（原地，arcpy 会自己建字段）。"""
    import arcpy

    desc = arcpy.Describe(target)
    if str(getattr(desc, "shapeType", "")).lower() not in ("point", "multipoint"):
        raise WorkerError(f"添加 XY 坐标只适用于点（或多点）要素，输入是 "
                          f"{getattr(desc, 'shapeType', '未知类型')}")
    arcpy.management.AddXY(target)
    created = [n for n in field_names(target) if n.upper().startswith("POINT_")]
    return {"in_features": target, "added_fields": created, "fields": field_names(target)}


def _add_geometry_attributes(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """原地添加几何属性字段（面积 / 长度 / 周长 / 中心点…）。"""
    import arcpy

    props = params.get("geometry_properties")
    if not props:
        props = ["AREA"]
    elif isinstance(props, str):
        props = [p.strip().upper() for p in props.replace(";", ",").split(",") if p.strip()]
    else:
        props = [str(p).strip().upper() for p in props]
    for unit_key in ("length_unit", "area_unit"):
        unit = params.get(unit_key)
        if unit and str(unit).upper() not in GEOMETRY_UNITS:
            raise WorkerError(f"{unit_key} 不认识：{unit!r}；可用单位：{'、'.join(GEOMETRY_UNITS)}")

    unknown = [p for p in props if p not in GEOMETRY_PROPERTIES]
    if unknown:
        raise WorkerError(f"几何属性 {'、'.join(unknown)} 不认识；可用："
                          f"{'、'.join(GEOMETRY_PROPERTIES)}")

    # 实测：经纬度数据不接受平面的面积 / 长度（arcpy 直接报「参数无效」），
    # 必须换成大地线版本，所以这里自动替换并说明。
    is_geographic = str(getattr(arcpy.Describe(target).spatialReference, "type", "")
                        ).lower() == "geographic"
    substituted: Dict[str, str] = {}
    if is_geographic:
        final: List[str] = []
        for prop in props:
            replacement = PLANAR_TO_GEODESIC.get(prop)
            if replacement:
                substituted[prop] = replacement
                final.append(replacement)
            else:
                final.append(prop)
        props = final

    before = set(field_names(target))
    arcpy.management.AddGeometryAttributes(target, props, params.get("length_unit"),
                                          params.get("area_unit"), params.get("coordinate_system"))
    added = [n for n in field_names(target) if n not in before]
    result: Dict[str, Any] = {
        "in_features": target, "added_fields": added, "fields": field_names(target),
        "note": "字段名由 arcpy 决定：面积 AREA_AR / AREA_GEO、周长 PERIMETER、"
                "中心点 CENTROID_X / CENTROID_Y 等",
    }
    if substituted:
        result["substituted"] = substituted
        result["substituted_note"] = (
            "数据是经纬度坐标系，平面面积/长度在椭球上没有意义，已自动换成大地线算法："
            + "、".join(f"{k}→{v}" for k, v in substituted.items())
        )
    return result


def _repair_geometry(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """原地修复几何（自相交、空几何、环方向错误等）。"""
    import arcpy

    validation = str(params.get("validation_method") or "ESRI").upper()
    if validation not in ("ESRI", "OGC"):
        raise WorkerError(f"validation_method 只能是 ESRI / OGC，收到 {validation!r}")
    table = unique_name("arcgis_mcp_check")
    before = -1
    try:
        with contextlib.suppress(Exception):
            arcpy.management.CheckGeometry(target, table, validation)
            before = int(arcpy.management.GetCount(table)[0])
    finally:
        with contextlib.suppress(Exception):
            arcpy.management.Delete(table)
    arcpy.management.RepairGeometry(target, bool(params.get("delete_null", False)), validation)
    return {"in_features": target, "problems_before": before,
            "note": ("已就地修复几何（修复后几何可能有微小变化）" if before != 0
                     else "几何本身没问题，未做改动")}


def _delete_dataset(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """删除数据集本身（不可逆，必须显式 confirm=true）。"""
    import arcpy

    if not bool(params.get("confirm")):
        raise WorkerError(
            "删除数据集是不可逆操作，必须显式传 confirm=true 才会执行；"
            f"要删除的目标：{target}"
        )
    arcpy.management.Delete(target)
    return {"deleted": target, "note": "已删除（不可恢复）"}


def _delete_features(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import arcpy
    if not params.get('confirm'):
        raise WorkerError('删除记录需要 confirm=true；仅在新副本中删除，输入数据保留。')
    is_table = str(arcpy.Describe(target).dataType).lower() in ('table', 'dbasetable')
    before = int(arcpy.management.GetCount(target)[0])
    where = params.get('where')
    layer = None
    try:
        selected = target
        if where:
            layer = unique_name('mcp_delete')
            make = arcpy.management.MakeTableView if is_table else arcpy.management.MakeFeatureLayer
            make(target, layer, where)
            selected = layer
        delete = arcpy.management.DeleteRows if is_table else arcpy.management.DeleteFeatures
        delete(selected)
    finally:
        if layer:
            with contextlib.suppress(Exception):
                arcpy.management.Delete(layer)
    after = int(arcpy.management.GetCount(target)[0])
    return {'deleted_count': before - after, 'feature_count': after,
            'count_before': before, 'count_after': after, 'where': where}


def _append(target: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """把来源数据追加进 target（**target 原地增长**）。

    这里不产出新数据集，所以归在「原地编辑」里 —— 否则会被「输出已存在」的检查拦住
    （target 本来就已经存在）。
    """
    import arcpy

    sources = params.get("in_features") or []
    if isinstance(sources, str):
        sources = [sources]
    if not sources:
        raise WorkerError("追加需要 in_features：要追加进来的一个或多个数据集")
    schema_type = str(params.get("schema_type") or "NO_TEST").upper()
    if schema_type not in ("TEST", "NO_TEST"):
        raise WorkerError(f"schema_type 只能是 TEST / NO_TEST，收到 {schema_type!r}")
    before = int(arcpy.management.GetCount(target)[0])
    arcpy.management.Append(sources, target, schema_type, params.get("field_mapping"),
                            params.get("subtype"), params.get("expression"),
                            params.get("match_fields"))
    after = int(arcpy.management.GetCount(target)[0])
    return {"in_features": target, "appended_from": sources,
            "count_before": before, "count_after": after,
            "appended_count": after - before}


# --------------------------------------------------------------------------
register("append", "追加到已有数据集（目标原地增长）", _append,
         allowed={"in_features", "schema_type", "field_mapping", "subtype",
                  "expression", "match_fields"})
register("delete_dataset", "删除数据集本身（不可逆，需 confirm=true）", _delete_dataset,
         allowed={"confirm"})
register('delete_features', '复制后按条件删除记录', _delete_features, allowed={'confirm', 'where'})
register("add_field", "加字段（原地）", _add_field,
         allowed={"field", "field_type", "field_length", "field_alias", "field_is_nullable"})
register("calculate_fields", "批量赋值 / 字段计算（原地）", _calculate_fields,
         allowed={"assignments", "where", "field_type", "expression_type", "code_block"})
register("delete_field", "删字段（原地，不可恢复）", _delete_field, allowed={"field"})
register("alter_field", "改字段名 / 别名 / 类型（原地）", _alter_field,
         allowed={"field", "new_name", "new_alias", "field_type", "field_length"})
register("add_xy_coordinates", "给点要素加 POINT_X / POINT_Y（原地）", _add_xy_coordinates)
register("add_geometry_attributes", "加几何属性字段（面积/长度/周长/中心点，原地）",
         _add_geometry_attributes,
         allowed={"geometry_properties", "length_unit", "area_unit", "coordinate_system"})
register("repair_geometry", "修复几何（原地）", _repair_geometry,
         allowed={"delete_null", "validation_method"})
