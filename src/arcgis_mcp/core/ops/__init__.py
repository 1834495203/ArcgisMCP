"""ops —— 会写数据的 arcpy 操作（按功能域分模块）。

**这里的代码运行在 ArcGIS Pro 自带的 Python 3.9 里**，所以：
- 只能 import 标准库和 arcpy，**绝不能 import mcp 或 arcgis_mcp 包里的其他模块**
  （那会连带 import 需要 Python ≥3.10 的 mcp SDK，worker 会直接崩）；
- 每个模块提供一个操作表，key 是操作名，value 是 {"summary","run","min_inputs","allowed"}。

两类操作：
- `GEOPROCESSING`（in → out）：产生新数据集，统一带输出容器自动创建 / 覆盖保护 /
  同路径拦截 / 输出摘要；
- `EDITING`（编辑算法）：只修改外层安全流程提供的副本（字段操作、修复几何等）。

加一个操作：在对应分类模块里写个 `_xxx(inputs, out, params)` 函数并登记到该模块的表里，
`ops/__init__.py` 会自动汇总；服务端再写一个工具类包一下即可。
"""

from __future__ import annotations

from typing import Any, Dict

from .common import WorkerError, brief, field_names, path_exists  # noqa: F401
from . import attributes, cartography, conversion, management, meta, raster, statistics, vector

#: in → out 的操作表（键就是 MCP 工具里传的 operation）
GEOPROCESSING: Dict[str, Dict[str, Any]] = {}
for module in (vector, management, cartography, raster, conversion, statistics):
    for name, spec in module.OPERATIONS.items():
        if name in GEOPROCESSING:
            raise RuntimeError(f"操作名重复：{name}")
        GEOPROCESSING[name] = spec

#: 编辑算法表；公开调用由 op_edit 先复制，再执行这些算法
EDITING: Dict[str, Dict[str, Any]] = dict(attributes.OPERATIONS)

#: 通用层（搜索/查看工具，仅调用已安全适配的工具），由 worker 直接注册
META: Dict[str, Any] = dict(meta.META_OPERATIONS)

__all__ = ["GEOPROCESSING", "EDITING", "META", "WorkerError", "brief", "field_names",
           "path_exists"]
