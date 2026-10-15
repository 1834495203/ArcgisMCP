"""栅格工具：栅格↔矢量、重分类、掩膜提取、复制/重采样/投影、分区统计。

栅格输出建议写成 `.tif` / `.img` 这类文件（`.gdb` 里也能放栅格数据集，但路径写法不同）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from ..core.mcp_compat import Context
from ..core.resolver import ResolutionError, resolve_input_path
from ..core.tool import Tool
from ..core.worker import WorkerError
from .base import GeoProcessTool


class DescribeData(Tool):
    """描述数据集（矢量 / 表 / 栅格都能用，只读）。"""

    name = "describe_data"
    needs_project = True

    def describe_data(self, ctx: Context, dataset: str) -> str:
        """查看任意数据集的元信息：类型、几何、记录数、坐标系、范围、字段列表；
        栅格还会给出波段数、像元大小、像元类型、压缩方式。

        什么时候用它：
        - 想知道一个栅格的分辨率 / 波段数（用 read_* 读不了栅格）；
        - 想确认数据有没有坐标系、是点还是面，而不用把记录读出来；
        - 想快速看字段名（等价于 read_shp_data 的 limit=0，但矢量栅格通用）。

        参数 dataset: 支持 "./data/x.gdb/要素类"、"./data/x.gdb"（库里第一个数据集）、
            "./shp/roads.shp"、"./rasters/landcover.tif"。
        """
        view = self.view(ctx)
        try:
            path = resolve_input_path(dataset, view.settings, "要描述的数据集")
        except ResolutionError as exc:
            return self.error("ResolutionError", exc, view, dataset=dataset)
        try:
            result = self.arcpy("describe", ctx=ctx, path=str(path))
        except WorkerError as exc:
            return self.error("ArcPyError", exc, view, dataset=str(path))
        return self.dumps({"ok": True, **self.project_block(view), **result})


class RasterToPolygon(GeoProcessTool):
    """栅格转面。"""

    name = "raster_to_polygon"
    operation = "raster_to_polygon"

    def raster_to_polygon(self, ctx: Context, in_raster: str, out_features: str,
                          simplify: bool = True, raster_field: Optional[str] = None,
                          create_multipart: bool = True, max_vertices: Optional[int] = None,
                          overwrite: bool = False) -> str:
        """把栅格转成面要素。

        in_raster: 输入栅格，例如 "./rasters/landcover.tif"（gdb 里的栅格也行）。
        out_features: 输出面要素路径，例如 "./out/栅格面.shp"。
            注意**输出是 shapefile 时没有面积字段**，要按面积筛选得先 add_field + calculate_fields。
        simplify: 默认 true，简化边界（更平滑但不严格贴合像元）。
        raster_field: 用栅格属性表里的哪个字段当分类依据（输出字段名就是它，默认 gridcode）。
        create_multipart: 默认 true；false 则同一值的**不相邻**区域拆成多个面。
        max_vertices: 单个面最多顶点数，用于限制超大面。
        """
        params: Dict[str, Any] = {"simplify": bool(simplify),
                                  "create_multipart": bool(create_multipart)}
        if raster_field:
            params["raster_field"] = raster_field
        if max_vertices is not None:
            params["max_vertices"] = int(max_vertices)
        return self.run_geoprocess(ctx, in_features=[in_raster], out_features=out_features,
                                   params=params, overwrite=overwrite)


class RasterToPoint(GeoProcessTool):
    """栅格转点（每个像元一个点）。"""

    name = "raster_to_point"
    operation = "raster_to_point"

    def raster_to_point(self, ctx: Context, in_raster: str, out_features: str,
                        raster_field: Optional[str] = None, overwrite: bool = False) -> str:
        """把栅格每个像元转成一个点（属性带像元值）。

        raster_field: 用哪个字段当点值（默认栅格自身的 Value）。
        用途：需要按像元值做点统计 / 抽样时；面太大太密时也常用点代替。
        """
        params: Dict[str, Any] = {}
        if raster_field:
            params["raster_field"] = raster_field
        return self.run_geoprocess(ctx, in_features=[in_raster], out_features=out_features,
                                   params=params, overwrite=overwrite)


class PolygonToRaster(GeoProcessTool):
    """矢量转栅格。"""

    name = "polygon_to_raster"
    operation = "polygon_to_raster"

    def polygon_to_raster(self, ctx: Context, in_features: str, out_features: str,
                          value_field: str, cell_size: float,
                          cell_assignment: str = "CELL_CENTER",
                          priority_field: Optional[str] = None,
                          overwrite: bool = False) -> str:
        """把面要素转成栅格。

        value_field: **必填**，用哪个字段的值当像元值（例如 "gridcode"、
            或 add_geometry_attributes 生成的面转点…注意字段必须是数值型）。
        cell_size: **必填**，像元大小（单位与数据一致）。经纬度数据要给小数，例如 0.0008。
        cell_assignment: CELL_CENTER（默认，按像元中心落在哪个面取值）/
            MAXIMUM_AREA（取面积占比最大的）/ MAXIMUM_COMBINED_AREA。
        priority_field: 可选，重叠时按这个字段的值优先。
        """
        params: Dict[str, Any] = {"value_field": value_field, "cell_size": float(cell_size),
                                  "cell_assignment": cell_assignment}
        if priority_field:
            params["priority_field"] = priority_field
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class Reclassify(GeoProcessTool):
    """栅格重分类。"""

    name = "reclassify"
    operation = "reclassify"

    def reclassify(self, ctx: Context, in_raster: str, out_features: str, remap: str,
                   reclass_field: Optional[str] = None, missing_values: Optional[str] = None,
                   overwrite: bool = False) -> str:
        """把栅格的某些值映射成新值。

        remap: **必填**，「原值 新值」用分号分隔；也可以写区间「起 止 新值」。
            例："0 1; 1 2; 2 2"（0→1，1→2，2→2）；
            例："0 5 1; 6 10 2"（0~5→1，6~10→2）。
        reclass_field: 依据哪个字段（默认 Value）。
        missing_values: 没在 remap 里出现的值怎么处理，默认 NODATA（也可是 "DATA" 或某个数）。
        """
        params: Dict[str, Any] = {"remap": remap}
        if reclass_field:
            params["reclass_field"] = reclass_field
        if missing_values:
            params["missing_values"] = missing_values
        return self.run_geoprocess(ctx, in_features=[in_raster], out_features=out_features,
                                   params=params, overwrite=overwrite)


class ExtractByMask(GeoProcessTool):
    """按掩膜提取。"""

    name = "extract_by_mask"
    operation = "extract_by_mask"

    def extract_by_mask(self, ctx: Context, in_raster: str, mask: str, out_features: str,
                        extraction_area: str = "INSIDE", overwrite: bool = False) -> str:
        """按掩膜（矢量或栅格）提取范围内的像元，范围外变 NoData。

        in_raster: 要提取的栅格；mask: 掩膜数据（面要素或栅格）；
        extraction_area: INSIDE（默认，留掩膜内）/ OUTSIDE（留掩膜外）。
        """
        return self.run_geoprocess(ctx, in_features=[in_raster, mask], out_features=out_features,
                                   params={"extraction_area": extraction_area},
                                   overwrite=overwrite)


class CopyRaster(GeoProcessTool):
    """复制栅格 / 转格式。"""

    name = "copy_raster"
    operation = "copy_raster"

    def copy_raster(self, ctx: Context, in_raster: str, out_features: str,
                    pixel_type: Optional[str] = None, format: Optional[str] = None,
                    nodata_value: Optional[str] = None, background_value: Optional[str] = None,
                    overwrite: bool = False) -> str:
        """复制栅格（也用于换格式、换存储、改像元类型）。

        pixel_type: 可选，例如 "8_BIT_UNSIGNED"、"16_BIT_SIGNED"、"32_BIT_FLOAT"。
        format: 可选，输出格式（例如 "TIFF"、"CRF"、"IMAGINE Image"）。
        nodata_value / background_value: 可选，设置 NoData 或背景值。
        """
        params: Dict[str, Any] = {}
        for key, value in (("pixel_type", pixel_type), ("format", format),
                           ("nodata_value", nodata_value), ("background_value", background_value)):
            if value not in (None, ""):
                params[key] = value
        return self.run_geoprocess(ctx, in_features=[in_raster], out_features=out_features,
                                   params=params, overwrite=overwrite)


class Resample(GeoProcessTool):
    """栅格重采样。"""

    name = "resample"
    operation = "resample"

    def resample(self, ctx: Context, in_raster: str, out_features: str, cell_size: float,
                 resampling_type: str = "NEAREST", overwrite: bool = False) -> str:
        """改栅格分辨率（像元大小）。

        cell_size: **必填**，目标像元大小。变大 = 降分辨率（文件更小）。
        resampling_type: NEAREST（默认，类别数据必须用它，不会产生新值）/
            BILINEAR / CUBIC（连续数据更平滑）/ MAJORITY（多数值）。
        """
        return self.run_geoprocess(ctx, in_features=[in_raster], out_features=out_features,
                                   params={"cell_size": float(cell_size),
                                           "resampling_type": resampling_type},
                                   overwrite=overwrite)


class ProjectRaster(GeoProcessTool):
    """栅格投影。"""

    name = "project_raster"
    operation = "project_raster"

    def project_raster(self, ctx: Context, in_raster: str, out_features: str,
                       spatial_reference: Union[str, int],
                       resampling_type: Optional[str] = None, cell_size: Optional[float] = None,
                       overwrite: bool = False) -> str:
        """把栅格换到另一个坐标系（改坐标）。

        spatial_reference: 目标坐标系（WKID 或名字）；cell_size: 可选，输出像元大小。
        """
        params: Dict[str, Any] = {"spatial_reference": spatial_reference}
        if resampling_type:
            params["resampling_type"] = resampling_type
        if cell_size is not None:
            params["cell_size"] = float(cell_size)
        return self.run_geoprocess(ctx, in_features=[in_raster], out_features=out_features,
                                   params=params, overwrite=overwrite)


class ZonalStatistics(GeoProcessTool):
    """分区统计。"""

    name = "zonal_statistics"
    operation = "zonal_statistics"

    def zonal_statistics(self, ctx: Context, in_features: str, in_value_raster: str,
                         out_features: str, zone_field: str, statistics_type: str = "ALL",
                         overwrite: bool = False) -> str:
        """按分区矢量统计栅格值，输出统计表（每个区一行，带统计结果列）。

        in_features: 分区矢量（面）；zone_field: **必填**，标识每个区的字段，例如 "NAME"。
        in_value_raster: 被统计的栅格。
        statistics_type: ALL（默认，输出全部统计量）/ MEAN / SUM / MAXIMUM / MINIMUM /
            MAJORITY / MEDIAN / RANGE / STD / VARIETY / MINORITY。
        用途：每个行政区的平均高程、每个地块的平均坡度这类统计。
        """
        return self.run_geoprocess(ctx, in_features=[in_features, in_value_raster],
                                   out_features=out_features,
                                   params={"zone_field": zone_field,
                                           "statistics_type": statistics_type},
                                   overwrite=overwrite)
