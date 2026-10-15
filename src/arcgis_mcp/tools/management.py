"""数据管理与字段编辑工具。

- 产出新数据集：复制 / 合并 / 追加 / 投影 / 定义投影 / 几何检查 / 创建 / 要素几何转换 /
  排序 / 消除 / 融合 / 更新 …
- **原地修改**：加字段 / 赋值 / 删字段 / 改字段 / 加 XY / 加几何属性 / 修复几何 / 删除数据集
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from ..core.mcp_compat import Context
from .base import EditTool, GeoProcessTool


# ==========================================================================
# 产出新数据集
# ==========================================================================
class CopyFeatures(GeoProcessTool):
    """复制数据集。"""

    name = "copy_features"
    operation = "copy_features"

    def copy_features(self, ctx: Context, in_features: str, out_features: str,
                      overwrite: bool = False) -> str:
        """复制一份数据集（也用来做 shp ↔ gdb 互转、给数据换存储位置）。

        例：copy_features(in_features="./data/shp/roads.shp",
                        out_features="./out/新库.gdb/roads")   # shp 进 gdb
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   overwrite=overwrite)


class Merge(GeoProcessTool):
    """合并。"""

    name = "merge"
    operation = "merge"

    def merge(self, ctx: Context, in_features: List[str], out_features: str,
              add_source: bool = False, overwrite: bool = False) -> str:
        """把多个同类型数据集**上下拼**成一个（字段按名字对应）。

        in_features: 数组，例如 ["./data/a.shp", "./data/b.shp"]，至少两个。
        add_source: true 时额外加一个字段记下每条来自哪个输入。
        注意：这是纵向拼接；横向叠属性请用 spatial_join。
        """
        params: Dict[str, Any] = {"add_source": bool(add_source)}
        return self.run_geoprocess(ctx, in_features=in_features, out_features=out_features,
                                   params=params, overwrite=overwrite)


class Append(EditTool):
    """追加（目标原地增长）。"""

    name = "append"
    operation = "append"

    def append(self, ctx: Context, in_features: Union[str, List[str]], target: str,
               schema_type: str = "NO_TEST", expression: Optional[str] = None,
               match_fields: Optional[List[str]] = None) -> str:
        """把数据追加进一个**已存在**的数据集（复制 target 后在新版本中追加）。

        in_features: 要追加进来的数据（一个或数组）。
        target: 追加到哪个已有数据集 —— **输入保留，返回追加后的新版本**。
        schema_type: TEST（字段必须完全一致）/ NO_TEST（默认，能对应上就填）。
        expression: 可选 SQL 表达式，给新加进来的记录赋固定值。
        match_fields: 可选，字段名不一致时手工指定对应关系，例如 ["A,B", "C,D"]。
        返回值里带 count_before / count_after / appended_count，能直接看出加了多少条。
        想输出成新数据集而不是改 target，请用 merge。
        """
        params: Dict[str, Any] = {"schema_type": schema_type}
        if expression:
            params["expression"] = expression
        if match_fields:
            params["match_fields"] = list(match_fields)
        sources = [in_features] if isinstance(in_features, str) else list(in_features)
        return self.run_edit(ctx, in_features=target, params=params,
                             extra_inputs=sources, label="被追加的目标数据集")


class Project(GeoProcessTool):
    """投影转换。"""

    name = "project"
    operation = "project"

    def project(self, ctx: Context, in_features: str, out_features: str,
                spatial_reference: Union[str, int], transform_method: Optional[str] = None,
                overwrite: bool = False) -> str:
        """把数据换到另一个坐标系（真正改坐标）。

        spatial_reference: 目标坐标系，可以给 WKID（如 4326、3857、4547）或名字
            （如 "WGS 1984 UTM Zone 50N"）。
        注意：数据**没有**坐标系时应该用 define_projection 先声明，再投影。
        """
        params: Dict[str, Any] = {"spatial_reference": spatial_reference}
        if transform_method:
            params["transform_method"] = transform_method
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class DefineProjection(GeoProcessTool):
    """定义投影。"""

    name = "define_projection"
    operation = "define_projection"

    def define_projection(self, ctx: Context, in_features: str, out_features: str,
                          spatial_reference: Union[str, int], overwrite: bool = False) -> str:
        """给数据**声明**坐标系（坐标数值不变，只标注它是什么坐标系）。

        适用于「数据本来就是 WGS84，但文件里没写坐标系」的情况。
        如果坐标数值本身就是错的（需要真正换算），请用 project。
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params={"spatial_reference": spatial_reference},
                                   overwrite=overwrite)


class CheckGeometry(GeoProcessTool):
    """几何检查。"""

    name = "check_geometry"
    operation = "check_geometry"

    def check_geometry(self, ctx: Context, in_features: str, out_features: str,
                       validation_method: str = "ESRI", overwrite: bool = False) -> str:
        """检查几何问题（自相交、空几何、环方向错误等），输出问题清单表。

        输出的表里有 FEATURE_ID / PROBLEM 字段；没有任何问题时表是空的（feature_count=0），
        说明数据干净。发现问题后可以用 repair_geometry 修复。
        validation_method: ESRI（默认）或 OGC。
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params={"validation_method": validation_method},
                                   overwrite=overwrite)


class CreateGdb(GeoProcessTool):
    """创建文件地理数据库。"""

    name = "create_gdb"
    operation = "create_gdb"

    def create_gdb(self, ctx: Context, out_features: str, version: str = "CURRENT",
                   overwrite: bool = False) -> str:
        """创建一个空的文件地理数据库。

        out_features 要以 .gdb 结尾，例如 "./out/新成果.gdb"，所在目录会自动建。
        之后往这个库里写要素类时，直接写 "./out/新成果.gdb/要素类名" 即可
        （其实不建也能写，服务端会自动建缺的 gdb，这个工具用于需要显式创建的场景）。
        一次流程复用一个输出库；已有库可直接使用，不要每个步骤建一个新库。
        不要为了解决某个要素类重名或被锁而覆盖整个 GDB。
        """
        return self.run_geoprocess(ctx, in_features=[], out_features=out_features,
                                   params={"version": version}, overwrite=overwrite)


class CreateFeatureClass(GeoProcessTool):
    """创建空要素类。"""

    name = "create_feature_class"
    operation = "create_feature_class"

    def create_feature_class(self, ctx: Context, out_features: str,
                             geometry_type: Optional[str] = None, template: Optional[str] = None,
                             spatial_reference: Optional[Union[str, int]] = None,
                             overwrite: bool = False) -> str:
        """创建一个空要素类 / shapefile。

        geometry_type: POINT / MULTIPOINT / POLYLINE / POLYGON / MULTIPATCH
            （给了 template 时可以省略，会用模板的几何类型和字段结构）。
        template: 可选，照它的字段结构建（例如照着已有成果建一个同结构的空图层）。
        spatial_reference: 可选，坐标系（WKID 或名字）。
        """
        params: Dict[str, Any] = {}
        if geometry_type:
            params["geometry_type"] = geometry_type
        if spatial_reference is not None:
            params["spatial_reference"] = spatial_reference
        return self.run_geoprocess(ctx, in_features=[template] if template else [],
                                   out_features=out_features, params=params, overwrite=overwrite)


class CreateTable(GeoProcessTool):
    """创建空表。"""

    name = "create_table"
    operation = "create_table"

    def create_table(self, ctx: Context, out_features: str, template: Optional[str] = None,
                     overwrite: bool = False) -> str:
        """创建一个空表（可以照已有表的字段结构建）。"""
        return self.run_geoprocess(ctx, in_features=[template] if template else [],
                                   out_features=out_features, overwrite=overwrite)


class FeatureToPoint(GeoProcessTool):
    """要素转点。"""

    name = "feature_to_point"
    operation = "feature_to_point"

    def feature_to_point(self, ctx: Context, in_features: str, out_features: str,
                         inside: bool = False, overwrite: bool = False) -> str:
        """把面/线要素转成点（用几何中心点代表）。

        inside: 默认 false 用几何重心（可能落在面外，例如 C 形面）；
            设 true 用保证落在面内的点（INSIDE）。
        """
        params: Dict[str, Any] = {"point_location": "INSIDE" if inside else "CENTROID"}
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class FeatureToLine(GeoProcessTool):
    """要素转线。"""

    name = "feature_to_line"
    operation = "feature_to_line"

    def feature_to_line(self, ctx: Context, in_features: Union[str, List[str]], out_features: str,
                        attributes: bool = True, cluster_tolerance: Optional[float] = None,
                        overwrite: bool = False) -> str:
        """把面边界转成线（多个输入时公共边界会被合并处理）。

        in_features: 一个或多个面图层（数组）；attributes: 是否保留属性。
        """
        params: Dict[str, Any] = {"attributes": "ATTRIBUTES" if attributes else "NO_ATTRIBUTES"}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=in_features, out_features=out_features,
                                   params=params, overwrite=overwrite)


class MinimumBoundingGeometry(GeoProcessTool):
    """最小包围几何。"""

    name = "minimum_bounding_geometry"
    operation = "minimum_bounding_geometry"

    def minimum_bounding_geometry(self, ctx: Context, in_features: str, out_features: str,
                                  geometry_type: str = "RECTANGLE_BY_AREA",
                                  group_option: str = "ALL",
                                  group_field: Optional[List[str]] = None,
                                  add_geometry: bool = False, overwrite: bool = False) -> str:
        """算最小包围几何：最小外接矩形 / 凸包 / 最小外接圆 / 包络矩形。

        geometry_type: RECTANGLE_BY_AREA（默认，面积最小的外接矩形）/
            RECTANGLE_BY_WIDTH（长边最小的）/ CONVEX_HULL（凸包）/ CIRCLE（最小外接圆）/
            ENVELOPE（正交包络矩形），带 _GEODESIC 后缀的是大地线版本。
        group_option: NONE（每个要素一个）/ ALL（默认，全部合成一个）/
            LIST（按 group_field 分组）。
        group_field: group_option=LIST 时用，例如 ["ZONE"]。
        add_geometry: 是否在结果里加上包围几何的宽高面积等字段。
        """
        params: Dict[str, Any] = {"geometry_type": geometry_type, "group_option": group_option,
                                  "add_geometry": bool(add_geometry)}
        if group_field:
            params["group_field"] = list(group_field)
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class Sort(GeoProcessTool):
    """排序。"""

    name = "sort"
    operation = "sort"

    def sort(self, ctx: Context, in_features: str, out_features: str, sort_field: List[str],
             overwrite: bool = False) -> str:
        """按字段排序输出（读取记录时也可以直接用 order_by 参数排序）。

        sort_field: **必填**，例如 ["POP DESC"] 或 ["ZONE ASC", "AREA_KM2 DESC"]。
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params={"sort_field": list(sort_field)}, overwrite=overwrite)


class MultipartToSinglepart(GeoProcessTool):
    """多部件转单部件。"""

    name = "multipart_to_singlepart"
    operation = "multipart_to_singlepart"

    def multipart_to_singlepart(self, ctx: Context, in_features: str, out_features: str,
                                overwrite: bool = False) -> str:
        """把一个要素里的多个分离部分拆成多个要素（属性复制，要素数会变多）。"""
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   overwrite=overwrite)


class Dissolve(GeoProcessTool):
    """融合。"""

    name = "dissolve"
    operation = "dissolve"

    def dissolve(self, ctx: Context, in_features: str, out_features: str,
                 dissolve_field: Optional[List[str]] = None,
                 statistics_fields: Optional[List[str]] = None, multi_part: bool = True,
                 overwrite: bool = False) -> str:
        """按字段把要素合并：字段值相同的合成一个。

        dissolve_field: 按哪些字段融合，例如 ["TL", "YL", "TS", "TZ"]；不给则全部合成一个。
        statistics_fields: 可选统计，格式 ["POP SUM", "AREA MAX"]。
        multi_part: 默认 true，融合结果可以是多部件；false 则拆成单部件。
        """
        params: Dict[str, Any] = {"multi_part": bool(multi_part)}
        if dissolve_field:
            params["dissolve_field"] = list(dissolve_field)
        if statistics_fields:
            params["statistics_fields"] = list(statistics_fields)
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)


class Eliminate(GeoProcessTool):
    """消除。"""

    name = "eliminate"
    operation = "eliminate"

    def eliminate(self, ctx: Context, in_features: str, out_features: str, where: str,
                  merge_with: str = "LENGTH", overwrite: bool = False) -> str:
        """把符合条件的小面（碎面、细长面）并进相邻的面，用来清理叠加分析产生的碎片。

        where: **必填**，选出要消除哪些面，例如 "AREA_MU < 15"、"Shape_Area < 0.1"。
            字段名用真实字段名；**注意面积单位**（1 亩 = 666.6667 平方米，15 亩 = 10000 平方米）。
            经纬度数据没有现成面积字段，先 add_field + calculate_fields 算一个（见 README）。
        merge_with: LENGTH（默认，并进共享边界最长的邻面）或 AREA（并进面积最大的邻面）。
        可以安全地重复执行：没选中任何要素时不报错，输出是原样副本、eliminated_count 为 0。
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params={"where": where, "merge_with": merge_with},
                                   overwrite=overwrite)


class UpdateFeatures(GeoProcessTool):
    """更新。"""

    name = "update_features"
    operation = "update"

    def update_features(self, ctx: Context, in_features: str, update_features: str,
                        out_features: str, keep_borders: bool = True,
                        cluster_tolerance: Optional[float] = None,
                        overwrite: bool = False) -> str:
        """用 update_features 去更新 in_features，结果写到**新数据集**。

        **不会改 in_features 本身**：这是 arcpy Update 的语义（输入 + 更新要素 → 新输出）。
        in_features: 被更新的面图层；update_features: 用来更新的面图层；out_features: 输出路径。
        keep_borders: 默认 true 保留更新要素的外边界；false 则不保留（与输入合并）。
        """
        params: Dict[str, Any] = {"keep_borders": "BORDERS" if keep_borders else "NO_BORDERS"}
        if cluster_tolerance is not None:
            params["cluster_tolerance"] = float(cluster_tolerance)
        return self.run_geoprocess(ctx, in_features=[in_features, update_features],
                                   out_features=out_features, params=params, overwrite=overwrite)


# ==========================================================================
# 原地修改（字段 / 几何 / 删除）
# ==========================================================================
class AddField(EditTool):
    """加字段（复制后编辑）。"""

    name = "add_field"
    operation = "add_field"

    def add_field(self, ctx: Context, in_features: str, field: str, field_type: str,
                  field_length: Optional[int] = None, field_alias: Optional[str] = None,
                  field_is_nullable: bool = True) -> str:
        """给数据集**复制后在新版本中**新增字段。

        field: 字段名，例如 "AREA_MU"；field_type: TEXT / DOUBLE / FLOAT / SHORT / LONG /
            BIGINTEGER / DATE / GUID。
        field_length: TEXT 类型的最长字符数（不给默认 254；arcpy 默认只有 50，中文会截断）。
        field_alias: 字段别名（ArcGIS 里显示的列名）。
        字段已存在时会报错（不会重复加）。
        """
        params: Dict[str, Any] = {"field": field, "field_type": field_type,
                                  "field_is_nullable": bool(field_is_nullable)}
        if field_length:
            params["field_length"] = int(field_length)
        if field_alias:
            params["field_alias"] = field_alias
        return self.run_edit(ctx, in_features=in_features, params=params)


class CalculateFields(EditTool):
    """批量赋值（复制后编辑）。"""

    name = "calculate_fields"
    operation = "calculate_fields"

    def calculate_fields(self, ctx: Context, in_features: str, assignments: Dict[str, str],
                         where: Optional[str] = None, field_type: Optional[str] = None,
                         expression_type: str = "PYTHON3", code_block: Optional[str] = None) -> str:
        """按条件批量给字段赋值（**原地修改**，一次可以赋多个字段）。

        assignments: 「字段名 -> 表达式」，例如
            {"TL": "'草毡土'", "YL": "'薄草毡土'", "AREA_MU": "!Shape_Area! / 666.6667"}
            文本值要带引号（"'草毡土'"）；引用字段用 !字段名!；数字直接写。
            算几何用 shape 表达式：面积 "!shape.geodesicArea@SQUAREMETERS!"（经纬度也能用）、
            投影数据也可用 "!shape.area@SQUAREMETERS!"；换算亩除以 666.6667。
        where: 可选，只改满足条件的要素，例如 "gridcode = 0"。
        field_type: 字段不存在时用它自动建（TEXT / DOUBLE / LONG…）；不给而字段又不存在则报错。
        expression_type: PYTHON3（默认）/ SQL / ARCADE。
        """
        params: Dict[str, Any] = {
            "assignments": {str(k): str(v) for k, v in (assignments or {}).items()},
            "expression_type": expression_type,
        }
        if where:
            params["where"] = where
        if field_type:
            params["field_type"] = field_type
        if code_block:
            params["code_block"] = code_block
        return self.run_edit(ctx, in_features=in_features, params=params)


class DeleteField(EditTool):
    """删字段（复制后编辑）。"""

    name = "delete_field"
    operation = "delete_field"

    def delete_field(self, ctx: Context, in_features: str, field: Union[str, List[str]]) -> str:
        """**复制后在新版本中**删除字段（输入原件保留）。field 可以是一个名字或名字数组，例如 ["A", "B"]。"""
        return self.run_edit(ctx, in_features=in_features, params={"field": field})


class AlterField(EditTool):
    """改字段（复制后编辑）。"""

    name = "alter_field"
    operation = "alter_field"

    def alter_field(self, ctx: Context, in_features: str, field: str,
                    new_name: Optional[str] = None, new_alias: Optional[str] = None,
                    field_type: Optional[str] = None, field_length: Optional[int] = None) -> str:
        """**复制后在新版本中**改字段名 / 别名 / 类型。

        field: 要改的字段；new_name: 新字段名；new_alias: 新别名（ArcGIS 显示用）；
        field_type / field_length: 改类型或长度（可能失败，取决于数据里已有值能否转换）。
        """
        params: Dict[str, Any] = {"field": field}
        if new_name:
            params["new_name"] = new_name
        if new_alias:
            params["new_alias"] = new_alias
        if field_type:
            params["field_type"] = field_type
        if field_length:
            params["field_length"] = int(field_length)
        return self.run_edit(ctx, in_features=in_features, params=params)


class AddXyCoordinates(EditTool):
    """加点坐标（复制后编辑）。"""

    name = "add_xy_coordinates"
    operation = "add_xy_coordinates"

    def add_xy_coordinates(self, ctx: Context, in_features: str) -> str:
        """复制**点**要素后在新版本加上 POINT_X / POINT_Y 两个字段（记录每个点的坐标）。"""
        return self.run_edit(ctx, in_features=in_features)


class AddGeometryAttributes(EditTool):
    """加几何属性字段（复制后编辑）。"""

    name = "add_geometry_attributes"
    operation = "add_geometry_attributes"

    def add_geometry_attributes(self, ctx: Context, in_features: str,
                                geometry_properties: Optional[List[str]] = None,
                                area_unit: Optional[str] = None,
                                length_unit: Optional[str] = None) -> str:
        """**复制后在新版本中**添加几何属性字段（面积 / 长度 / 周长 / 中心点 / 顶点数…）。

        geometry_properties: 数组，例如 ["AREA", "PERIMETER_LENGTH", "CENTROID"]；
            默认 ["AREA"]。可用：AREA、AREA_GEODESIC、PERIMETER_LENGTH、LENGTH、
            LENGTH_GEODESIC、CENTROID、CENTROID_INSIDE、EXTENT、PART_COUNT、POINT_COUNT、
            MIN_Z、MAX_Z（带 _GEODESIC 的是大地线算法，经纬度数据更准）。
        area_unit / length_unit: 面积/长度单位，例如 "SQUARE_METERS"、"HECTARES"、"ACRES"、
            "METERS"、"KILOMETERS"。不指定则用数据自身单位。
        加出来的字段名由 arcpy 决定（AREA / PERIMETER_LENGTH / CENTROID_X 等），
        返回值里的 added_fields 会告诉你实际叫什么。
        """
        params: Dict[str, Any] = {}
        if geometry_properties:
            params["geometry_properties"] = list(geometry_properties)
        if area_unit:
            params["area_unit"] = area_unit
        if length_unit:
            params["length_unit"] = length_unit
        return self.run_edit(ctx, in_features=in_features, params=params)


class RepairGeometry(EditTool):
    """修复几何（复制后编辑）。"""

    name = "repair_geometry"
    operation = "repair_geometry"

    def repair_geometry(self, ctx: Context, in_features: str, delete_null: bool = False,
                        validation_method: str = "ESRI") -> str:
        """**复制后在新版本中**修复几何问题（自相交、空几何、环方向错误等）。

        什么时候用：intersect / union / buffer 报「几何无效」时；或者从别处导入的数据有问题时。
        返回值里的 problems_before 是修复前的问题数（-1 表示检查失败）；几何本身没问题时不做改动。
        想先看有什么问题、不动数据，用 check_geometry（输出问题清单表）。
        """
        params: Dict[str, Any] = {"delete_null": bool(delete_null),
                                  "validation_method": validation_method}
        return self.run_edit(ctx, in_features=in_features, params=params)


class DeleteDataset(EditTool):
    """归档删除数据集，移除原路径并保留历史。"""

    name = "delete_dataset"
    operation = "delete_dataset"

    def delete_dataset(self, ctx: Context, in_features: str, confirm: bool = False) -> str:
        """删除指定数据集路径：先将完整原件改名归档，原路径随后不存在。

        confirm 必须是 true 才会执行 —— 默认 false 会直接报错，避免误删。
        output / previous_output 是保留的历史数据路径，original_path_exists=false。
        不擦除历史数据；如需只删除部分记录，使用 delete_features。
        删除库内要素类时，in_features 写成 ./results.gdb/要素类名，不要只传 .gdb。
        归档仍可见是正常保留，不代表删除失败；出现锁错误时报告原文，不能通过新建库解决。
        """
        return self.run_edit(ctx, in_features=in_features,
                             params={"confirm": bool(confirm)}, label="要删除的数据集")


class DeleteFeatures(EditTool):
    name = 'delete_features'
    operation = 'delete_features'

    def delete_features(self, ctx: Context, in_features: str,
                        where: Optional[str] = None, confirm: bool = False) -> str:
        """先复制数据集，再在副本中删除记录；输入原件保留。

        where 为空表示删除副本中的全部记录；confirm=true 才执行。
        返回 source 是输入路径，output 是删除记录后的新版本。
        本工具不会移除原要素类；要移除原路径请用 delete_dataset(confirm=true)。
        """
        return self.run_edit(ctx, in_features=in_features,
                             params={'where': where, 'confirm': bool(confirm)})
