"""格式转换操作：表 ↔ Excel / CSV，要素 ↔ JSON，坐标表转点。

对应 ArcGIS 的 Conversion Tools。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import WorkerError, path_exists
from .tabular import table_output

OPERATIONS: Dict[str, Dict[str, Any]] = {}


def register(name: str, summary: str, run, min_inputs: int = 1, allowed=()) -> None:
    OPERATIONS[name] = {"summary": summary, "run": run,
                        "min_inputs": min_inputs, "allowed": set(allowed)}


def _table_to_excel(inputs: List[str], out: str,
                    params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把要素类 / 表导出成 Excel（.xls / .xlsx）。"""
    import arcpy

    if not str(out).lower().endswith((".xls", ".xlsx")):
        raise WorkerError(f"输出要写成 Excel 文件，例如 ./out/统计.xlsx（收到 {out}）")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    arcpy.conversion.TableToExcel(
        inputs, out,
        # 实测：这两个参数是 ALIAS / NAME、YES / NO，不是 YES / NO 与布尔
        # 实测：这个参数是 ALIAS / NAME（表头用别名还是真实字段名）
        "ALIAS" if params.get("use_field_alias", True) else "NAME",
        # 实测：这个参数是 DESCRIPTION / CODE（域和子类型的描述还是代码）
        "DESCRIPTION" if params.get("use_domain_and_subtype_description", False) else "CODE")


def _excel_to_table(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 Excel 表转成要素表 / 独立表。"""
    import arcpy

    arcpy.conversion.ExcelToTable(inputs[0], out, params.get("sheet"),
                                  params.get("field_names_row"), params.get("cell_range"))


def _export_table(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """导出表 / 要素类属性为 CSV / TXT / DBF。

    实测：TableToTable 直接接受带扩展名的名字（"a.csv" 会生成 a.csv，另外附带一个
    a.csv.xml），不需要先写临时名再改名。
    """
    import arcpy

    suffix = Path(out).suffix.lower()
    if suffix not in (".csv", ".txt", ".dbf"):
        raise WorkerError(f"导出表的目标要是 .csv / .txt / .dbf，例如 ./out/汇总.csv（收到 {out}）")
    parent = Path(out).parent
    parent.mkdir(parents=True, exist_ok=True)
    with table_output(out, params.get('clean_csv', True)) as table:
        arcpy.conversion.TableToTable(str(inputs[0]), str(Path(table).parent), Path(table).name,
                                      params.get('where'))
    return {"format": suffix.lstrip("."), "output": out, 'clean_csv': params.get('clean_csv', True)}


def _features_to_json(inputs: List[str], out: str,
                      params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """要素导出为 JSON / GeoJSON。

    实测：这个版本没有「是否带几何」的参数（几何总是带上），第 7 个参数是
    outputToWGS84（WGS84 / KEEP_INPUT_SR），第 8 个是 use_field_alias（ALIAS / NAME）。
    """
    import arcpy

    # 实测：geoJSON=GEOJSON 时 arcpy 会把输出**强制写成 .geojson**（不管你请求什么后缀），
    # 所以这里先把后缀归一化，并把真实路径报回去，免得调用方去读一个不存在的文件。
    want_geojson = bool(params.get("geojson", False))
    suffix = ".geojson" if want_geojson else ".json"
    requested = str(out)
    if not requested.lower().endswith(suffix):
        out = str(Path(requested).with_suffix(suffix))
    if Path(out).suffix.lower() not in (".json", ".geojson"):
        raise WorkerError(f"输出要写成 .json / .geojson，例如 ./out/结果.json（收到 {requested}）")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    arcpy.conversion.FeaturesToJSON(
        inputs[0], out,
        "FORMATTED" if params.get("formatted", True) else "NOT_FORMATTED",
        "Z_VALUES" if params.get("include_z") else "NO_Z_VALUES",
        "M_VALUES" if params.get("include_m") else "NO_M_VALUES",
        # 实测：这个枚举是 GEOJSON / NO_GEOJSON
        "GEOJSON" if params.get("geojson", False) else "NO_GEOJSON",
        "WGS84" if params.get("to_wgs84", False) else "KEEP_INPUT_SR",
        # 实测：这个参数是 USE_FIELD_ALIAS / USE_FIELD_NAME
        "USE_FIELD_ALIAS" if params.get("use_field_alias", False) else "USE_FIELD_NAME")
    result: Dict[str, Any] = {"output": out}
    if out != requested:
        result["requested_output"] = requested
        result["output_note"] = (f"GeoJSON 模式下 arcpy 会强制用 .geojson 后缀，"
                                 f"实际输出是 {out}")
    return result


def _json_to_features(inputs: List[str], out: str,
                      params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """JSON / GeoJSON 转要素类。"""
    import arcpy

    if not str(inputs[0]).lower().endswith((".json", ".geojson")):
        raise WorkerError(f"输入要是 .json / .geojson 文件，收到：{inputs[0]}")
    parent = Path(out).parent
    parent.mkdir(parents=True, exist_ok=True)
    arcpy.conversion.JSONToFeatures(str(inputs[0]), str(out), params.get("geometry_type"))


def _xy_table_to_point(inputs: List[str], out: str,
                       params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """坐标表转点（表里两列经纬度 / XY → 点要素）。"""
    import arcpy

    x_field = params.get("x_field")
    y_field = params.get("y_field")
    if not x_field or not y_field:
        raise WorkerError('坐标表转点需要 x_field 和 y_field，例如 x_field="LON", y_field="LAT"')
    arcpy.management.XYTableToPoint(inputs[0], out, x_field, y_field,
                                    params.get("z_field"), params.get("coordinate_system"))


def _raster_to_other_format(inputs: List[str], out: str,
                            params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """栅格批量转换成其它格式（输出到目录）。"""
    import arcpy

    raster_format = str(params.get("raster_format") or "TIFF").upper()
    allowed = ("TIFF", "IMAGINE Image", "BMP", "GIF", "PNG", "JPEG", "JP2000", "CRF", "MRF")
    if raster_format not in allowed:
        raise WorkerError(f"raster_format 不认识：{raster_format!r}\n可用：{'、'.join(allowed)}")
    Path(out).mkdir(parents=True, exist_ok=True)
    arcpy.conversion.RasterToOtherFormat(inputs, out, raster_format)


register("table_to_excel", "要素/表导出为 Excel", _table_to_excel, min_inputs=1,
         allowed={"use_field_alias", "use_domain_and_subtype_description"})
register("excel_to_table", "Excel 转表", _excel_to_table, min_inputs=1,
         allowed={"sheet", "field_names_row", "cell_range"})
register("export_table", "导出表为 CSV / TXT / DBF", _export_table, min_inputs=1,
         allowed={"where", 'clean_csv'})
register("features_to_json", "要素导出为 JSON / GeoJSON", _features_to_json, min_inputs=1,
         allowed={"formatted", "include_z", "include_m", "geojson", "to_wgs84",
                  "use_field_alias"})
register("json_to_features", "JSON / GeoJSON 转要素", _json_to_features, min_inputs=1,
         allowed={"geometry_type"})
register("xy_table_to_point", "坐标表转点要素", _xy_table_to_point, min_inputs=1,
         allowed={"x_field", "y_field", "z_field", "coordinate_system"})
register("raster_to_other_format", "栅格批量转格式（输出到目录）", _raster_to_other_format,
         min_inputs=1, allowed={"raster_format"})
