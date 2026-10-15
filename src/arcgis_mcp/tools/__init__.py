"""所有 MCP 工具。

**加一个新工具 = 三步：**

1. 在对应功能域的模块里写一个继承基类的类。基类按「是否产出新数据集」选：

   - **产出新数据集** → 继承 `base.GeoProcessTool`（如 buffer、union、project）：
     声明 `operation`（对应 core/ops 里注册的操作名），方法里调 `self.run_geoprocess(...)`；
   - **ArcPy 原地编辑类操作** → 继承 `base.EditTool`（如 add_field、repair_geometry）：
     声明 `operation`，方法里调 `self.run_edit(...)`；安全层先复制再操作，保留原件；
   - 读取类 → 直接继承 `core.tool.Tool`（如 read_gdb_data）。

       class MyTool(GeoProcessTool):
           name = "my_tool"            # = MCP 工具名 = 下面那个方法名（基类会检查）
           operation = "my_operation"  # 对应 core/ops 里注册的操作
           needs_project = True        # 需要项目根目录

           def my_tool(self, ctx: Context, in_features: str, out_features: str,
                       limit: int = 10, overwrite: bool = False) -> str:
               \"\"\"工具说明（原样发给 AI；前置条件不用写，基类会拼）。\"\"\"
               return self.run_geoprocess(ctx, in_features=[in_features],
                                          out_features=out_features,
                                          params={"limit": limit}, overwrite=overwrite)

2. 把类加进下面的 ALL_TOOLS（顺序 = AI 看到的工具列表顺序）；
3. arcpy 侧还没有这个操作的话，在 `core/ops/<域>.py` 里实现并 register
   （那些文件运行在 ArcGIS Pro 的 Python 3.9 里，只能依赖标准库和 arcpy）。

约定（基类会检查，写错启动就报错）：`name` 必须等于公开方法名；
方法签名的类型注解就是给 AI 的入参 schema，所以别用 `**kwargs`；
`ctx: Context` 由 SDK 注入、不占 schema 参数，放第一位。

工具清单（按功能域，顺序 = AI 看到的顺序）：
  项目与探索  use_project / list_datasources / describe_data / arcgis_status
  读取        list_gdb_datasets / read_gdb_data / list_shp_datasets / read_shp_data
  分析        intersect / union / erase / clip / identity / symmetrical_difference /
              buffer / spatial_join / near / select / select_by_location /
              summary_statistics / frequency
  数据管理    copy_features / merge / append / project / define_projection /
              check_geometry / create_gdb / create_feature_class / create_table /
              feature_to_point / feature_to_line / minimum_bounding_geometry / sort /
              multipart_to_singlepart / dissolve / eliminate / update_features
  字段与几何编辑（复制后执行）
              add_field / calculate_fields / delete_field / alter_field /
              add_xy_coordinates / add_geometry_attributes / repair_geometry /
              delete_dataset
  制图综合    smooth / simplify / densify
  栅格        raster_to_polygon / raster_to_point / polygon_to_raster / reclassify /
              extract_by_mask / copy_raster / resample / project_raster / zonal_statistics
  格式转换    table_to_excel / excel_to_table / export_table / features_to_json /
              json_to_features / xy_table_to_point / raster_to_other_format
"""

from __future__ import annotations

from typing import Sequence, Type

from ..core.tool import Services, Tool
from .analysis import (
    Buffer,
    Clip,
    Erase,
    Frequency,
    Identity,
    Intersect,
    Near,
    Select,
    SelectByLocation,
    SpatialJoin,
    SummaryStatistics,
    SymmetricalDifference,
    UnionTool,
)
from .cartography import Densify, Simplify, Smooth
from .conversion import (
    ExcelToTable,
    ExportTable,
    FeaturesToJson,
    JsonToFeatures,
    RasterToOtherFormat,
    TableToExcel,
    XyTableToPoint,
)
from .gdb import ListGdbDatasets, ReadGdbData
from .meta import DescribeArcpyTool, ListArcpyTools, RunArcpyCode, RunArcpyTool
from .management import (
    AddField,
    AddGeometryAttributes,
    AddXyCoordinates,
    AlterField,
    Append,
    CalculateFields,
    CheckGeometry,
    CopyFeatures,
    CreateFeatureClass,
    CreateGdb,
    CreateTable,
    DefineProjection,
    DeleteDataset,
    DeleteFeatures,
    DeleteField,
    Dissolve,
    Eliminate,
    FeatureToLine,
    FeatureToPoint,
    Merge,
    MinimumBoundingGeometry,
    MultipartToSinglepart,
    Project,
    RepairGeometry,
    Sort,
    UpdateFeatures,
)
from .raster import (
    CopyRaster,
    DescribeData,
    ExtractByMask,
    PolygonToRaster,
    ProjectRaster,
    RasterToPoint,
    RasterToPolygon,
    Reclassify,
    Resample,
    ZonalStatistics,
)
from .shp import ListShpDatasets, ReadShpData
from .workspace import ArcGisStatus, ListDataSources, UseProject
from .history import Lineage, Explain, JobStatus, JobResult

#: 注册顺序 = AI 看到的工具列表顺序：先告诉服务端在哪干活、再看有什么、再读、再写
#: （通用层工具 ListArcpyTools / DescribeArcpyTool / RunArcpyTool 在上面「项目与探索」那段，
#:   因为固定工具不够用时应该先想到它们）
ALL_TOOLS: Sequence[Type[Tool]] = (
    # 项目与探索
    UseProject,
    ListDataSources,
    DescribeData,
    ArcGisStatus,
    Lineage,
    Explain,
    JobStatus,
    JobResult,
    # 通用 arcpy 能力：固定工具不够用时的出口
    ListArcpyTools,
    DescribeArcpyTool,
    RunArcpyTool,
    # 读取
    ListGdbDatasets,
    ReadGdbData,
    ListShpDatasets,
    ReadShpData,
    # 分析
    Intersect,
    UnionTool,
    Erase,
    Clip,
    Identity,
    SymmetricalDifference,
    Buffer,
    SpatialJoin,
    Near,
    Select,
    SelectByLocation,
    SummaryStatistics,
    Frequency,
    # 数据管理
    CopyFeatures,
    Merge,
    Append,
    Project,
    DefineProjection,
    CheckGeometry,
    CreateGdb,
    CreateFeatureClass,
    CreateTable,
    FeatureToPoint,
    FeatureToLine,
    MinimumBoundingGeometry,
    Sort,
    MultipartToSinglepart,
    Dissolve,
    Eliminate,
    UpdateFeatures,
    # 字段与几何编辑（复制后执行）
    AddField,
    CalculateFields,
    DeleteField,
    AlterField,
    AddXyCoordinates,
    AddGeometryAttributes,
    RepairGeometry,
    DeleteDataset,
    DeleteFeatures,
    # 制图综合
    Smooth,
    Simplify,
    Densify,
    # 栅格
    RasterToPolygon,
    RasterToPoint,
    PolygonToRaster,
    Reclassify,
    ExtractByMask,
    CopyRaster,
    Resample,
    ProjectRaster,
    ZonalStatistics,
    # 格式转换
    TableToExcel,
    ExcelToTable,
    ExportTable,
    FeaturesToJson,
    JsonToFeatures,
    XyTableToPoint,
    RasterToOtherFormat,
)


def register_all(server, services: Services) -> list:
    """实例化所有工具并注册到 MCP server，返回实例列表（便于自检/测试）。

    run_arcpy_code 默认不注册；旧配置可注册兼容入口，但审计保留模式仍拒绝执行代码。
    """
    from ..core.logs import get_logger

    classes = list(ALL_TOOLS)
    if services.settings.allow_arcpy_code:
        classes.append(RunArcpyCode)
        get_logger("server").warning(
            "allow_arcpy_code=true：已注册兼容入口 run_arcpy_code，"
            "但审计保留模式禁止执行任意代码；请使用安全工具适配器"
        )
    instances = []
    for tool_class in classes:
        tool = tool_class(services)
        tool.register(server)
        instances.append(tool)
    return instances


def tool_names() -> list:
    return [t.name for t in ALL_TOOLS]


__all__ = ["ALL_TOOLS", "register_all", "tool_names", "Tool", "Services"]
