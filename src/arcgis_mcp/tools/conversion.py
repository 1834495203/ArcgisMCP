"""格式转换工具：表 ↔ Excel / CSV，要素 ↔ JSON，坐标表转点，栅格批量转格式。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from ..core.mcp_compat import Context
from .base import GeoProcessTool


class TableToExcel(GeoProcessTool):
    """导出 Excel。"""

    name = "table_to_excel"
    operation = "table_to_excel"

    def table_to_excel(self, ctx: Context, in_features: Union[str, List[str]], out_features: str,
                       use_field_alias: bool = True, overwrite: bool = False) -> str:
        """把要素类 / 表的属性导出成 Excel（.xls / .xlsx）。

        in_features: 一个或多个数据集（数组时每个在 Excel 里一个 sheet）。
        out_features: 要写成 Excel 文件，例如 "./out/统计结果.xlsx"（父目录会自动建）。
        use_field_alias: 默认 true，Excel 表头用字段别名（中文别名更好读）。
        注意：Excel 单元格上限约 100 万行；行数很多时用 export_table 导 CSV。
        """
        return self.run_geoprocess(ctx, in_features=in_features, out_features=out_features,
                                   params={"use_field_alias": bool(use_field_alias)},
                                   overwrite=overwrite)


class ExcelToTable(GeoProcessTool):
    """导入 Excel。"""

    name = "excel_to_table"
    operation = "excel_to_table"

    def excel_to_table(self, ctx: Context, in_excel: str, out_features: str,
                       sheet: Optional[str] = None, field_names_row: Optional[int] = None,
                       cell_range: Optional[str] = None, overwrite: bool = False) -> str:
        """把 Excel 表转成表 / 要素属性表（输出可以是 .csv、.dbf，或 gdb 里的表）。

        in_excel: Excel 文件路径，例如 "./data/统计.xlsx"。
        sheet: sheet 名（不给则用第一个）。
        field_names_row: 第几行是表头（默认 1）。
        cell_range: 只读某个区域，例如 "A1:D20"。
        转成表后如果要做空间分析，通常还要 xy_table_to_point 变成点。
        """
        params: Dict[str, Any] = {}
        if sheet:
            params["sheet"] = sheet
        if field_names_row is not None:
            params["field_names_row"] = int(field_names_row)
        if cell_range:
            params["cell_range"] = cell_range
        return self.run_geoprocess(ctx, in_features=[in_excel], out_features=out_features,
                                   params=params, overwrite=overwrite)


class ExportTable(GeoProcessTool):
    """导出 CSV / TXT / DBF。"""

    name = "export_table"
    operation = "export_table"

    def export_table(self, ctx: Context, in_features: str, out_features: str,
                     where: Optional[str] = None, overwrite: bool = False, clean_csv: bool = True) -> str:
        """把要素类 / 表的属性导出成 CSV / TXT / DBF。

        out_features: 要以 .csv / .txt / .dbf 结尾，例如 "./out/汇总.csv"。
        where: 可选，只导出满足条件的记录，例如 "AREA_MU > 15"。
        行数很多（几十万以上）时用它比 table_to_excel 稳。
        CSV 默认输出单个 UTF-8 文件；clean_csv=false 允许 ArcPy 生成类型/元数据配套文件。
        """
        params: Dict[str, Any] = {'clean_csv': clean_csv}
        if where:
            params["where"] = where
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class FeaturesToJson(GeoProcessTool):
    """要素转 JSON。"""

    name = "features_to_json"
    operation = "features_to_json"

    def features_to_json(self, ctx: Context, in_features: str, out_features: str,
                         geojson: bool = False, include_z: bool = False,
                         include_m: bool = False, to_wgs84: bool = False,
                         use_field_alias: bool = False, overwrite: bool = False) -> str:
        """把要素导出成 JSON / GeoJSON（给别的程序或 web 用）。

        out_features: 要写成 .json（.geojson 也行），例如 "./out/成果.json"。
        geojson: true 输出标准 GeoJSON；默认 false 输出 ArcGIS JSON。
        to_wgs84: true 时把坐标转成 WGS84 再导出（默认 false，保持原坐标系）。
        use_field_alias: true 则 JSON 里用字段别名当键名（默认用真实字段名）。
        注意：这个版本没有「不带几何」的选项，几何总会一起导出。
        """
        params: Dict[str, Any] = {"geojson": bool(geojson), "include_z": bool(include_z),
                                  "include_m": bool(include_m), "to_wgs84": bool(to_wgs84),
                                  "use_field_alias": bool(use_field_alias)}
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class JsonToFeatures(GeoProcessTool):
    """JSON 转要素。"""

    name = "json_to_features"
    operation = "json_to_features"

    def json_to_features(self, ctx: Context, in_json: str, out_features: str,
                         geometry_type: Optional[str] = None, overwrite: bool = False) -> str:
        """把 JSON / GeoJSON 转成要素类。

        in_json: .json / .geojson 文件路径。
        geometry_type: 可选，强制指定几何类型（POINT / POLYLINE / POLYGON）。
        """
        params: Dict[str, Any] = {}
        if geometry_type:
            params["geometry_type"] = geometry_type
        return self.run_geoprocess(ctx, in_features=[in_json], out_features=out_features,
                                   params=params, overwrite=overwrite)


class XyTableToPoint(GeoProcessTool):
    """坐标表转点。"""

    name = "xy_table_to_point"
    operation = "xy_table_to_point"

    def xy_table_to_point(self, ctx: Context, in_features: str, out_features: str,
                          x_field: str, y_field: str, z_field: Optional[str] = None,
                          coordinate_system: Optional[Union[str, int]] = None,
                          overwrite: bool = False) -> str:
        """把带经纬度 / XY 两列的表转成点要素。

        in_features: 表或要素类（例如 Excel 转出来的表、CSV 表）。
        x_field / y_field: **必填**，坐标字段名，例如 x_field="LON", y_field="LAT"。
        coordinate_system: 可选，坐标字段用的是什么坐标系（WKID 或名字），
            例如 4326 表示经纬度。不给则结果没有坐标系。
        z_field: 可选，高程字段。
        """
        params: Dict[str, Any] = {"x_field": x_field, "y_field": y_field}
        if z_field:
            params["z_field"] = z_field
        if coordinate_system is not None:
            params["coordinate_system"] = coordinate_system
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class RasterToOtherFormat(GeoProcessTool):
    """栅格批量转格式。"""

    name = "raster_to_other_format"
    operation = "raster_to_other_format"

    def raster_to_other_format(self, ctx: Context, in_rasters: List[str], out_folder: str,
                               raster_format: str = "TIFF", overwrite: bool = False) -> str:
        """把一个或多个栅格批量转成指定格式，输出到目录。

        in_rasters: 数组，至少一个栅格。
        out_folder: 输出**目录**（不是文件名），例如 "./out/栅格导出"。
        raster_format: TIFF（默认）/ IMAGINE Image / PNG / JPEG / CRF / MRF / BMP / GIF / JP2000。
        """
        return self.run_geoprocess(ctx, in_features=in_rasters, out_features=out_folder,
                                   params={"raster_format": raster_format}, overwrite=overwrite)
