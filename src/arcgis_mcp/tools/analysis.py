"""分析工具：叠加分析、邻域分析、统计汇总（都会产出新数据集）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from ..core.mcp_compat import Context
from .base import GeoProcessTool


class Intersect(GeoProcessTool):
    """相交。"""

    name = "intersect"
    operation = "intersect"

    def intersect(self, ctx: Context, in_features: List[str], out_features: str,
                  join_attributes: str = "ALL", output_type: str = "INPUT",
                  cluster_tolerance: Optional[float] = None, overwrite: bool = False) -> str:
        """计算多个图层的交集，并把各层属性合并到结果里。

        in_features: **数组**，至少两个输入，例如
            ["./data/Demo.gdb/districts", "./data/shp/zones.shp"]。
        out_features: 输出路径，例如 "./out/相交.shp"。
        join_attributes: ALL（默认，保留全部属性）/ NO_FID / ONLY_FID。
        output_type: INPUT（默认，与输入同类型）/ LINE（只输出交线）/ POINT（只输出交点）。
        cluster_tolerance: 聚类容差（单位与数据一致），精度不一致时用来对齐。
        overwrite: 输出已存在时是否覆盖，默认 false。
        """
        params: Dict[str, Any] = {"join_attributes": join_attributes, "output_type": output_type}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=in_features, out_features=out_features,
                                   params=params, overwrite=overwrite)


class UnionTool(GeoProcessTool):
    """联合。

    类名故意不叫 Union：那会把 typing.Union 遮蔽掉，同模块里 `Union[float, str]`
    这种注解在求值时会报「type 'Union' is not subscriptable」（踩过）。
    MCP 工具名由 name 属性决定，类名随便叫。
    """

    name = "union"
    operation = "union"

    def union(self, ctx: Context, in_features: List[str], out_features: str,
              join_attributes: str = "ALL", gaps: bool = True,
              cluster_tolerance: Optional[float] = None, overwrite: bool = False) -> str:
        """求多个面图层的并集（保留所有输入的范围和边界）。

        in_features: 数组，至少两个面图层。
        out_features: 输出路径。
        join_attributes: ALL（默认）/ NO_FID / ONLY_FID。
        gaps: true（默认）会把被围住但没有归属的空白区域也作为要素输出。
        cluster_tolerance: 聚类容差。
        overwrite: 输出已存在时是否覆盖，默认 false。
        """
        params: Dict[str, Any] = {"join_attributes": join_attributes,
                                  "gaps": "GAPS" if gaps else "NO_GAPS"}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=in_features, out_features=out_features,
                                   params=params, overwrite=overwrite)


class Erase(GeoProcessTool):
    """擦除。"""

    name = "erase"
    operation = "erase"

    def erase(self, ctx: Context, in_features: str, erase_features: str, out_features: str,
              cluster_tolerance: Optional[float] = None, overwrite: bool = False) -> str:
        """从 in_features 中挖掉 erase_features 覆盖的部分（结果可能变成多部件）。

        in_features: 被擦除的图层；erase_features: 挖掉的范围；out_features: 输出路径。
        """
        params: Dict[str, Any] = {}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=[in_features, erase_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


class Clip(GeoProcessTool):
    """裁剪。"""

    name = "clip"
    operation = "clip"

    def clip(self, ctx: Context, in_features: str, clip_features: str, out_features: str,
             cluster_tolerance: Optional[float] = None, overwrite: bool = False) -> str:
        """按 clip_features 的范围裁剪 in_features（只保留范围内部分，属性不变）。

        和 erase 的区别：clip 留范围**内**，erase 挖掉范围**内**。
        """
        params: Dict[str, Any] = {}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=[in_features, clip_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


class Identity(GeoProcessTool):
    """标识。"""

    name = "identity"
    operation = "identity"

    def identity(self, ctx: Context, in_features: str, identity_features: str, out_features: str,
                 join_attributes: str = "ALL", relationship: str = "NO_RELATIONSHIPS",
                 cluster_tolerance: Optional[float] = None, overwrite: bool = False) -> str:
        """叠加并**保留 in_features 的全部范围**（重叠部分带上 identity 图层的属性）。

        和 intersect 的区别：intersect 只留公共部分，identity 保留输入全部范围。
        """
        params: Dict[str, Any] = {"join_attributes": join_attributes,
                                  "relationship": relationship}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=[in_features, identity_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


class SymmetricalDifference(GeoProcessTool):
    """对称差。"""

    name = "symmetrical_difference"
    operation = "symmetrical_difference"

    def symmetrical_difference(self, ctx: Context, in_features: str,
                               update_features: str, out_features: str,
                               join_attributes: str = "ALL",
                               cluster_tolerance: Optional[float] = None,
                               overwrite: bool = False) -> str:
        """取两个图层**不重叠**的部分（重叠区域被去掉）。

        in_features / update_features: 两个输入图层；out_features: 输出路径。
        """
        params: Dict[str, Any] = {"join_attributes": join_attributes}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=[in_features, update_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


class Buffer(GeoProcessTool):
    """缓冲区。"""

    name = "buffer"
    operation = "buffer"

    def buffer(self, ctx: Context, in_features: str, out_features: str,
               distance: Union[float, str], dissolve_option: str = "NONE",
               dissolve_field: Optional[List[str]] = None, line_side: Optional[str] = None,
               line_end_type: Optional[str] = None, method: Optional[str] = None,
               overwrite: bool = False) -> str:
        """生成缓冲区（点/线/面都能用）。

        distance: **必填**，缓冲距离。数字表示固定距离（单位与数据一致，例如 0.01 度或 100 米）；
            也可以给字段名（例如 "BUFFER_DIST"），那样每个要素用自己的距离。
        dissolve_option: NONE（默认，每个要素一个缓冲区）/ ALL（全部融合成一个）/
            LIST（按 dissolve_field 融合）。
        dissolve_field: dissolve_option=LIST 时必填，例如 ["ZONE"]。
        line_side: 线的缓冲方向 FULL（默认）/ LEFT / RIGHT / OUTSIDE_ONLY。
        line_end_type: 线端头 ROUND（默认）/ FLAT / SQUARE。
        method: PLANAR（默认）或 GEODESIC（大地线，经纬度数据算大范围时更准）。
        """
        params: Dict[str, Any] = {"distance": distance, "dissolve_option": dissolve_option}
        if dissolve_field:
            params["dissolve_field"] = list(dissolve_field)
        for key, value in (("line_side", line_side), ("line_end_type", line_end_type),
                           ("method", method)):
            if value:
                params[key] = value
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class SpatialJoin(GeoProcessTool):
    """空间连接。"""

    name = "spatial_join"
    operation = "spatial_join"

    def spatial_join(self, ctx: Context, target_features: str, join_features: str,
                     out_features: str, match_option: str = "INTERSECT",
                     join_operation: str = "JOIN_ONE_TO_ONE", join_type: str = "KEEP_ALL",
                     search_radius: Optional[float] = None,
                     distance_field_name: Optional[str] = None,
                     overwrite: bool = False) -> str:
        """按空间关系把 join_features 的属性连到 target_features 上（类似按位置做属性连接）。

        target_features: 目标图层（结果保留它的几何）；join_features: 被连接的图层。
        match_option: 空间关系，常用 INTERSECT（默认，相交）、WITHIN_A_DISTANCE（需给
            search_radius）、CONTAINS、WITHIN、CLOSEST、HAVE_THEIR_CENTER_IN 等。
        join_operation: JOIN_ONE_TO_ONE（默认，每个目标要素一条结果）/ JOIN_ONE_TO_MANY。
        join_type: KEEP_ALL（默认，没匹配上的目标要素也保留）/ KEEP_COMMON（只留匹配上的）。
        search_radius: match_option 用 WITHIN_A_DISTANCE 时必填。
        distance_field_name: 可选，把匹配距离写进这个字段。
        """
        params: Dict[str, Any] = {"match_option": match_option, "join_operation": join_operation,
                                  "join_type": join_type}
        if search_radius is not None:
            params["search_radius"] = float(search_radius)
        if distance_field_name:
            params["distance_field_name"] = distance_field_name
        return self.run_geoprocess(ctx, in_features=[target_features, join_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


class Near(GeoProcessTool):
    """近邻分析。"""

    name = "near"
    operation = "near"

    def near(self, ctx: Context, in_features: str, near_features: str, out_features: str,
             search_radius: Optional[float] = None, location: bool = False, angle: bool = False,
             method: str = "PLANAR", overwrite: bool = False) -> str:
        """给每个要素找出最近的要素并算距离。

        结果是 **in_features 的副本**，多了 NEAR_FID（最近要素的 ID）和 NEAR_DIST（距离）字段；
        location=true 再加 NEAR_X / NEAR_Y，angle=true 再加 NEAR_ANGLE。
        注意 arcpy 的 Near 会原地改数据，所以这里输出的是副本，原数据不动。
        """
        params: Dict[str, Any] = {
            "location": "LOCATION" if location else "NO_LOCATION",
            "angle": "ANGLE" if angle else "NO_ANGLE",
            "method": method,
        }
        if search_radius is not None:
            params["search_radius"] = float(search_radius)
        return self.run_geoprocess(ctx, in_features=[in_features, near_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


class SelectByLocation(GeoProcessTool):
    """按位置筛选导出。"""

    name = "select_by_location"
    operation = "select_by_location"

    def select_by_location(self, ctx: Context, in_features: str, select_features: str,
                           out_features: str, relation: str = "INTERSECT",
                           search_distance: Optional[float] = None, invert: bool = False,
                           overwrite: bool = False) -> str:
        """按空间关系从 in_features 里挑出要素导出。

        和 spatial_join 的区别：这个只挑要素（不连属性），结果就是 in_features 的子集。
        relation: INTERSECT（默认，相交）/ WITHIN / CONTAINS / WITHIN_A_DISTANCE（需给
            search_distance）/ TOUCHES / OVERLAPS / CROSSES / HAVE_THEIR_CENTER_IN 等。
        invert: true 表示反过来——挑选**不满足**关系的要素。
        """
        params: Dict[str, Any] = {"relation": relation, "invert": bool(invert)}
        if search_distance is not None:
            params["search_distance"] = float(search_distance)
        return self.run_geoprocess(ctx, in_features=[in_features, select_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


class Select(GeoProcessTool):
    """按属性筛选导出。"""

    name = "select"
    operation = "select"

    def select(self, ctx: Context, in_features: str, out_features: str, where: str,
               overwrite: bool = False) -> str:
        """按属性条件把一部分要素导出成新数据集。

        用法就是「把 B 里的寒冻土挑出来单独存一份」这类操作。
        where: **必填**，例如 "TYPE = '寒冻土'"、"gridcode = 0"、"AREA_MU < 15"。
            字段名用真实字段名（不确定就先 read_gdb_data / read_shp_data 的 limit=0 看字段）。
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params={"where": where}, overwrite=overwrite)


class SummaryStatistics(GeoProcessTool):
    """汇总统计。"""

    name = "summary_statistics"
    operation = "summary_statistics"

    def summary_statistics(self, ctx: Context, in_features: str, out_features: str,
                           statistics_fields: List[str], case_field: Optional[List[str]] = None,
                           overwrite: bool = False, clean_csv: bool = True) -> str:
        """对字段做汇总统计，输出一张统计表。

        statistics_fields: **必填**，格式 ["字段 统计方式"]，例如
            ["POP SUM", "AREA_KM2 MEAN", "NAME COUNT"]。
            统计方式：SUM / MEAN / MIN / MAX / STD / COUNT / FIRST / LAST / MEDIAN / RANGE / VARIANCE。
        case_field: 可选，按这些字段分组（每个组合一行），例如 ["ZONE"]。
        out_features 可写 ./结果.gdb/统计表，保留字段类型；CSV 默认只生成单个 UTF-8 文件。
        clean_csv=false 使用 ArcPy 原生 CSV（可能生成 XML/schema.ini，供类型识别）。
        """
        params: Dict[str, Any] = {"statistics_fields": list(statistics_fields), 'clean_csv': clean_csv}
        if case_field:
            params["case_field"] = list(case_field)
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class Frequency(GeoProcessTool):
    """频数统计。"""

    name = "frequency"
    operation = "frequency"

    def frequency(self, ctx: Context, in_features: str, out_features: str,
                  frequency_field: List[str], summary_fields: Optional[List[str]] = None,
                  overwrite: bool = False, clean_csv: bool = True) -> str:
        """统计字段取值的出现次数，输出一张表。

        frequency_field: **必填**，要统计的字段，例如 ["TYPE"] 或 ["TYPE", "ZONE"]。
        summary_fields: 可选，需要按组合求和的数值字段名，例如 ["AREA_MU"]。
        多字段按完整组合分组，FREQUENCY 为该组合的输入记录数；融合后的组合频数全为 1 正常。
        输出可写 ./结果.gdb/频数表以保留类型。CSV 默认单文件，clean_csv=false 保留原生配套文件。
        """
        params: Dict[str, Any] = {"frequency_field": list(frequency_field), 'clean_csv': clean_csv}
        if summary_fields:
            params["summary_fields"] = list(summary_fields)
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)
