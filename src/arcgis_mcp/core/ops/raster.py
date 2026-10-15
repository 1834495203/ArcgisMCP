"""栅格操作：栅格转矢量、矢量转栅格、重分类、掩膜提取、复制/重采样/投影、分区统计。

对应 ArcGIS 的 Conversion（栅格部分）与 Spatial Analyst（基础部分）。
注意：栅格不能放进 .gdb 的同级目录规则里（.gdb 里可以放栅格数据集，但要走 arcpy 的路径），
所以栅格输出建议写成 .tif / .img 这类文件。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .common import WorkerError, unique_name
from .tabular import table_output

OPERATIONS: Dict[str, Dict[str, Any]] = {}


def register(name: str, summary: str, run, min_inputs: int = 1, allowed=()) -> None:
    OPERATIONS[name] = {"summary": summary, "run": run,
                        "min_inputs": min_inputs, "allowed": set(allowed)}


def _is_raster(path: str) -> bool:
    import arcpy

    return str(arcpy.Describe(path).dataType).lower() in (
        "rasterdataset", "raster", "rasterband")


# --------------------------------------------------------------------------
def _raster_to_polygon(inputs: List[str], out: str,
                       params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    simplify = "SIMPLIFY" if params.get("simplify", True) else "NO_SIMPLIFY"
    multipart = ("MULTIPLE_OUTER_PART" if params.get("create_multipart", True)
                 else "SINGLE_OUTER_PART")
    max_vertices = params.get("max_vertices")
    arcpy.conversion.RasterToPolygon(inputs[0], out, simplify, params.get("raster_field"),
                                     multipart, int(max_vertices) if max_vertices else None)
    names = [field.name for field in arcpy.ListFields(out)]
    candidates = [params.get('raster_field'), 'gridcode', 'grid_code']
    value_field = next((name for candidate in candidates if candidate
                        for name in names if name.lower() == str(candidate).lower()), None)
    return {'output_value_field': value_field,
            'effective_parameters': {'simplify': simplify == 'SIMPLIFY',
                'create_multipart': multipart == 'MULTIPLE_OUTER_PART',
                'raster_field': params.get('raster_field') or 'Value'},
            'field_note': 'output_value_field 无法识别时请读取实际 fields；输入分类字段不保证输出同名。'}


def _raster_to_point(inputs: List[str], out: str,
                     params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    if not _is_raster(inputs[0]):
        raise WorkerError(f"栅格转点需要栅格输入，收到的是：{inputs[0]}")
    arcpy.conversion.RasterToPoint(inputs[0], out, params.get("raster_field"))


def _polygon_to_raster(inputs: List[str], out: str,
                       params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import arcpy

    value_field = params.get("value_field")
    if not value_field:
        raise WorkerError('矢量转栅格需要 value_field：用哪个字段的值当像元值（例如 "gridcode"）')
    cell_size = params.get("cell_size")
    if cell_size in (None, ""):
        raise WorkerError("矢量转栅格需要 cell_size（像元大小，单位与数据一致）")
    cell_assignment = str(params.get("cell_assignment") or "CELL_CENTER").upper()
    if cell_assignment not in ("CELL_CENTER", "MAXIMUM_AREA", "MAXIMUM_COMBINED_AREA"):
        raise WorkerError(f"cell_assignment 只能是 CELL_CENTER / MAXIMUM_AREA / "
                          f"MAXIMUM_COMBINED_AREA，收到 {cell_assignment!r}")
    arcpy.conversion.PolygonToRaster(inputs[0], value_field, out, cell_assignment,
                                     params.get("priority_field"), cell_size)


def _reclassify(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """重分类：把某些值映射成新值。

    remap 用「原值 新值; 原值 新值」这种紧凑写法，例如 "0 1; 1 2; 2 2"（也可用 "0 1 5 9" 表示 0-5 映射到 9）。
    """
    import arcpy

    remap_text = str(params.get("remap") or "").strip()
    if not remap_text:
        raise WorkerError('重分类需要 remap，例如 remap="0 1; 1 2; 2 2"（原值 新值，分号分隔多组）')
    remap = arcpy.sa.RemapValue(_parse_remap(remap_text))
    field = params.get("reclass_field")
    missing = params.get("missing_values")
    field = field if field else "Value"
    if missing in (None, ""):
        result = arcpy.sa.Reclassify(inputs[0], field, remap)
    else:
        result = arcpy.sa.Reclassify(inputs[0], field, remap, missing)
    result.save(out)


def _parse_remap(text: str) -> List[List[float]]:
    """把 "0 1; 1 2" 或 "0 5 9" 解析成 RemapValue 需要的 [[start, end, new], ...]。

    实测：arcpy.sa.RemapValue **不接受浮点行**（传 [[0.0,0.0,1.0]] 会报
    「无法设置参数 remap 中的输入」），所以能取整时一律给整数。
    """
    def number(raw: str) -> Any:
        value = float(raw)
        return int(value) if value.is_integer() else value

    groups = [g.strip() for g in text.replace("\n", ";").split(";") if g.strip()]
    rows: List[List[Any]] = []
    for group in groups:
        parts = [p for p in group.replace(",", " ").split() if p]
        if len(parts) == 2:
            value, new = number(parts[0]), number(parts[1])
            rows.append([value, value, new])
        elif len(parts) == 3:
            rows.append([number(parts[0]), number(parts[1]), number(parts[2])])
        else:
            raise WorkerError(f'remap 每一组要么是「原值 新值」，要么是「起 止 新值」，'
                              f'看不懂这一组：{group!r}')
    return rows


def _extract_by_mask(inputs: List[str], out: str,
                     params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按掩膜提取（用矢量或栅格当掩膜裁剪出范围内的像元）。"""
    import arcpy

    if not _is_raster(inputs[0]):
        raise WorkerError(f"按掩膜提取的输入必须是栅格，收到：{inputs[0]}")
    area = str(params.get("extraction_area") or "INSIDE").upper()
    if area not in ("INSIDE", "OUTSIDE"):
        raise WorkerError(f"extraction_area 只能是 INSIDE（掩膜内）/ OUTSIDE（掩膜外），收到 {area!r}")
    result = arcpy.sa.ExtractByMask(inputs[0], inputs[1], area)
    result.save(out)
    return {'extraction_area': area,
            'mask_semantics': '矢量掩膜按像元中心取舍，不保证矢量边界完整覆盖；业务精度由调用方核验。'}


def _copy_raster(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """复制栅格（也用于格式转换 / 设置压缩与背景值）。"""
    import arcpy

    if not _is_raster(inputs[0]):
        raise WorkerError(f"复制栅格需要栅格输入，收到：{inputs[0]}")
    arcpy.management.CopyRaster(inputs[0], out, params.get("config_keyword"),
                                params.get("background_value"), params.get("nodata_value"),
                                params.get("onebit_to_eightbit"), params.get("colormap_to_RGB"),
                                params.get("pixel_type"), params.get("scale_pixel_value"),
                                params.get("format"))


def _resample(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """重采样（改像元大小）。"""
    import arcpy

    cell_size = params.get("cell_size")
    if cell_size in (None, ""):
        raise WorkerError("重采样需要 cell_size（目标像元大小）")
    technique = str(params.get("resampling_type") or "NEAREST").upper()
    allowed = ("NEAREST", "BILINEAR", "CUBIC", "MAJORITY")
    if technique not in allowed:
        raise WorkerError(f"resampling_type 只能是 {'/'.join(allowed)}，收到 {technique!r}")
    arcpy.management.Resample(inputs[0], out, cell_size, technique)


def _project_raster(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """栅格投影转换。"""
    import arcpy

    sr = params.get("spatial_reference")
    if sr in (None, ""):
        raise WorkerError("栅格投影需要 spatial_reference（WKID 或坐标系名字）")
    from .management import _make_sr

    arcpy.management.ProjectRaster(inputs[0], out, _make_sr(sr),
                                   params.get("resampling_type"), params.get("cell_size"),
                                   params.get("geographic_transform"))


def _zonal_statistics(inputs: List[str], out: str,
                      params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """分区统计（按分区矢量统计栅格值，输出统计表）。"""
    import arcpy

    zone_field = params.get("zone_field")
    if not zone_field:
        raise WorkerError('分区统计需要 zone_field：分区数据里标识每个区的字段名（例如 "NAME"）')
    if not _is_raster(inputs[1]):
        raise WorkerError(f"分区统计的第二个输入必须是栅格（被统计的值栅格），收到：{inputs[1]}")
    statistics_type = str(params.get("statistics_type") or "ALL").upper()
    allowed = ("ALL", "MEAN", "MAJORITY", "MAXIMUM", "MEDIAN", "MINIMUM", "MINORITY",
               "RANGE", "STD", "SUM", "VARIETY")
    if statistics_type not in allowed:
        raise WorkerError(f"statistics_type 只能是 {'/'.join(allowed)}，收到 {statistics_type!r}")
    with table_output(out, params.get('clean_csv', True)) as table:
        arcpy.sa.ZonalStatisticsAsTable(inputs[0], zone_field, inputs[1], table,
                                      params.get("ignore_nodata", "DATA"), statistics_type,
                                      params.get("process_as_multidimensional"))
    return {'clean_csv': params.get('clean_csv', True), 'zone_field': zone_field,
            'statistics_type': statistics_type}


register("raster_to_polygon", "栅格转面", _raster_to_polygon, min_inputs=1,
         allowed={"simplify", "raster_field", "create_multipart", "max_vertices"})
register("raster_to_point", "栅格转点", _raster_to_point, min_inputs=1,
         allowed={"raster_field"})
register("polygon_to_raster", "矢量转栅格", _polygon_to_raster, min_inputs=1,
         allowed={"value_field", "cell_size", "cell_assignment", "priority_field"})
register("reclassify", "栅格重分类", _reclassify, min_inputs=1,
         allowed={"remap", "reclass_field", "missing_values"})
register("extract_by_mask", "按掩膜提取栅格", _extract_by_mask, min_inputs=2,
         allowed={"extraction_area"})
register("copy_raster", "复制栅格（格式转换/压缩）", _copy_raster, min_inputs=1,
         allowed={"config_keyword", "background_value", "nodata_value", "onebit_to_eightbit",
                  "colormap_to_RGB", "pixel_type", "scale_pixel_value", "format"})
register("resample", "栅格重采样（改分辨率）", _resample, min_inputs=1,
         allowed={"cell_size", "resampling_type"})
register("project_raster", "栅格投影转换", _project_raster, min_inputs=1,
         allowed={"spatial_reference", "resampling_type", "cell_size", "geographic_transform"})
register("zonal_statistics", "分区统计（矢量分区 + 栅格值 → 统计表）", _zonal_statistics,
         min_inputs=2,
         allowed={"zone_field", "statistics_type", "ignore_nodata", "process_as_multidimensional", "clean_csv"})
