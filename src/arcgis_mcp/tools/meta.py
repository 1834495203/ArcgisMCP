"""搜索本机 ArcPy 工具、查看参数与安全适配状态，执行已适配工具。

搜索目录覆盖范围不代表执行范围；固定封装优先，未知工具拒绝执行。
路径与参数解析在后台任务中完成，任意 Python 代码不能绕过审计保护。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core.mcp_compat import Context, ToolError
from ..core.resolver import (
    ResolutionError,
    looks_like_path,
    resolve_input_path,
    resolve_output_path,
)
from ..core.tool import Tool
from ..core.worker import WorkerError


class ListArcpyTools(Tool):
    """搜索 arcpy 工具。"""

    name = "list_arcpy_tools"
    needs_project = False          # 只是想看看有哪些工具，不需要项目根目录

    def list_arcpy_tools(self, ctx: Context, keyword: Optional[str] = None,
                         toolbox: Optional[str] = None, limit: int = 60) -> str:
        """搜索本机 ArcPy 工具目录，数量随安装和扩展变化，不代表可执行数量。

        默认用这里查询语法，再在受审计工作流节点中编写 ArcPy 代码。
        execution.allowed 表示兼容入口是否已有安全适配器，不表示 MCP 执行接口已经注册。
        run_arcpy_tool / run_unadapted_tool 仅在 tool_profile=legacy 时暴露；以实际工具列表为准。
        兼容 run_arcpy_tool 只允许 allowed=true，confirm 不能绕过分类。
        已适配仍受参数、输出、数据格式和许可证限制，不代表所有参数组合均已实测。

        参数
        ----
        keyword: 英文关键词，匹配工具名（子串），例如 "Dissolve"、"Tessellation"、
            "Topology"、"Interpolate"、"Raster"、"Statistics"。不给则列出全部（会截断）。
        toolbox: 只看某个工具箱，例如 "management"、"analysis"、"cartography"、
            "conversion"、"sa"（Spatial Analyst）、"3d"、"topology"、"stats"。
        limit: 最多返回多少个（默认 60）。

        返回每一项的 name（调用时用它）、toolbox 和 syntax（语法行，含参数名与内联可选值）。
        total_matched 为匹配目录数，classified_matched 为其中已安全分类数（均在截断前统计）。
        execution 包含 mode、constraints 和 validation；下一步用 describe_arcpy_tool 看执行限制及参数。
        """
        try:
            result = self.arcpy("list_arcpy_tools", ctx=ctx, keyword=keyword, toolbox=toolbox,
                                limit=int(limit))
        except WorkerError as exc:
            return self.error("ArcPyError", exc)
        return self.dumps({"ok": True, **result})


class DescribeArcpyTool(Tool):
    """查看任意 arcpy 工具的参数。"""

    name = "describe_arcpy_tool"
    needs_project = False

    def describe_arcpy_tool(self, ctx: Context, tool: str, include_usage: bool = True) -> str:
        """查看任意 arcpy 工具的完整参数：参数名、方向（输入/输出）、是否必填、
        数据类型、可选值、是否多值，以及语法行。

        用它来搞清「这个工具要传什么参数名、哪些必填、某参数只能填什么值」，
        默认在受审计工作流节点中使用这些参数编写 ArcPy 代码。
        execution 是兼容适配器状态；run_arcpy_tool / run_unadapted_tool 仅在 legacy 配置注册。
        兼容 run_arcpy_tool 只有 allowed=true 才能尝试，仍需遵守参数限制。
        参数可查询、安全适配均不代表所有参数组合已经实测。

        参数 tool: 工具名，三种写法都行 —— "GenerateTessellation"、
            "GenerateTessellation_management"、"management.GenerateTessellation"。

        返回里的 parameters 是逐个参数的细节；input_parameters / output_parameters
        直接告诉你哪些参数是输入、哪些是输出（输出参数会自动建目录、并做覆盖保护）。
        注意：当前是中文版 ArcGIS，allowed_values 可能是中文关键字（arcpy 也接受
        对应的英文关键字，例如 MULTI_PART / SINGLE_PART）。
        """
        try:
            result = self.arcpy("describe_arcpy_tool", ctx=ctx, tool=tool,
                                include_usage=bool(include_usage))
        except WorkerError as exc:
            return self.error("ArcPyError", exc, tool=tool)
        return self.dumps({"ok": True, **result})


#: 参数名里含这些词的「输出」按目录处理（会创建目录）
_DIR_HINTS = ("folder", "workspace", "dir", "path")
#: 数据集后缀：有这些后缀的按「数据集路径」处理
_DATASET_SUFFIXES = (".shp", ".gdb", ".mdb", ".tif", ".tiff", ".img", ".csv", ".txt",
                     ".dbf", ".json", ".geojson", ".xlsx", ".xls", ".lyrx", ".aprx",
                     ".xml", ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".crf")


class RunArcpyTool(Tool):
    """调用已完成安全适配的 arcpy 工具。"""

    name = "run_arcpy_tool"
    needs_project = True

    def run_arcpy_tool(self, ctx: Context, tool: str, parameters: Dict[str, Any],
                       overwrite: bool = False, confirm: bool = False) -> str:
        """调用已完成安全分类的 arcpy 工具；未知工具拒绝执行，避免绕过副本和审计。

        **用法：先 describe_arcpy_tool 确认 execution.allowed=true，检查 constraints、参数名和取值。**
        参数名以 describe 的结果为准；编辑操作先复制，所有历史成果保留。
        优先使用固定封装。安全分类不保证所有参数组合或业务结果正确，仍需核验坐标系、单位和结果。
        连续步骤复用同一输出 GDB，只换要素类名，不要每步创建新库或用新库绕过锁错误。

        参数
        ----
        tool: 工具名，例如 "GenerateTessellation"、"PairwiseDissolve_analysis"、
            "management.CreateFeatureclass"。
        parameters: 参数名 → 参数值。例如
            {"Output_Feature_Class": "./out/格网.shp", "Extent": "./data/Demo.gdb/districts",
             "Shape_Type": "六边形", "Size": 0.5}
            路径参数写 "./" 相对路径即可（服务端按项目根目录解析，输出目录会自动创建）；
            列表参数（多值）直接给数组。
        overwrite: false 时拒绝已有输出；true 时将旧目标归档到 previous_output，并替换指定路径。
        confirm: 删除记录、归档删除或写到项目/搜索根之外需要 true；不能绕过安全分类或擦除历史数据。

        提交后返回 job_id，用 job_status / job_result 获取最终结果。最终结果里的 outputs
        列出产出工具输出的实际路径、是否存在、要素数/几何类型/坐标系；编辑工具使用 output。
        （非 arcpy 格式的输出如 json/xlsx 只报文件大小）。

        常见组合示例
        ----
        先查再调：
            describe_arcpy_tool(tool="GenerateTessellation")   → 参数名是 Output_Feature_Class / Extent / Shape_Type / Size
            run_arcpy_tool(tool="GenerateTessellation",
                           parameters={"Output_Feature_Class": "./out/格网.shp",
                                       "Extent": "./data/Demo.gdb/districts",
                                       "Shape_Type": "六边形", "Size": 0.5})
        调用固定工具没有的 pairwise 版本：
            run_arcpy_tool(tool="PairwiseDissolve_analysis",
                           parameters={"in_features": "./data/shp/zones.shp",
                                       "out_feature_class": "./out/pw.shp",
                                       "dissolve_field": ["ZONE"]})
        """
        view = self.view(ctx)
        settings = view.settings
        if not isinstance(parameters, dict) or not parameters:
            raise ToolError(
                "parameters 不能为空：它是一个对象，键是参数名、值是参数值。\n"
                "先用 describe_arcpy_tool(tool=\"...\") 看该工具的输入/输出参数名，"
                "例如 parameters={\"in_features\": \"./data/x.shp\", \"out_feature_class\": \"./out/y.shp\"}"
            )

        # Metadata, path classification and execution all run inside the durable job.
        # No synchronous worker preflight can consume the client's 30-second budget.
        self.trace(f"提交 arcpy 工具 {tool}", ctx)
        try:
            result = self.arcpy("run_arcpy_tool", ctx=ctx, tool=tool, parameters=parameters,
                                allowed_roots=[str(view.root)] + [str(p) for p in settings.search_roots],
                                overwrite=bool(overwrite), confirm=bool(confirm))
        except WorkerError as exc:
            return self.error("ArcPyError", exc, tool=tool, parameters=parameters)

        if result.get('pending') or result.get('ok') is False:
            return self.dumps({**self.project_block(view), **result})

        return self.dumps({
            "ok": True,
            **self.project_block(view),
            **result,
            "usage_hint": "要读结果就用 read_gdb_data / read_shp_data / describe_data；"
                          "继续组合就用别的工具（固定工具或本工具）把它当输入。",
        })

    # ------------------------------------------------------------------
    def _resolve_outputs(self, value: Any, key: str, settings,
                         outside_roots: List[str]) -> Any:
        """输出参数：解析路径、建目录、检查是否在允许的目录内。"""
        if isinstance(value, (list, tuple)):
            return [self._resolve_outputs(v, key, settings, outside_roots) for v in value]
        if not isinstance(value, str) or not looks_like_path(value):
            return value          # 纯名字（例如 out_name="roads"）原样传

        try:
            path = resolve_output_path(value, settings)
        except ResolutionError:
            return value
        text = str(path)
        if self._outside_allowed_roots(path, settings):
            outside_roots.append(text)
        # 目录类输出（out_folder_path / workspace 这类，且没有数据集后缀）先建出来
        suffix = Path(text).suffix.lower()
        is_dir_param = any(hint in key.lower() for hint in _DIR_HINTS)
        if is_dir_param and suffix not in _DATASET_SUFFIXES:
            Path(text).mkdir(parents=True, exist_ok=True)
        return text

    def _resolve_inputs(self, value: Any, settings) -> Any:
        """输入参数：能解析成已有数据就换成绝对路径，否则原样传（让 arcpy 报错）。"""
        if isinstance(value, (list, tuple)):
            return [self._resolve_inputs(v, settings) for v in value]
        if not isinstance(value, str) or not looks_like_path(value):
            return value
        try:
            return str(resolve_input_path(value, settings))
        except ResolutionError:
            return value

    @staticmethod
    def _outside_allowed_roots(path: Path, settings) -> bool:
        roots = [Path(p) for p in settings.search_roots] + [Path.cwd()]
        try:
            target = path.resolve()
        except OSError:
            target = path
        for root in roots:
            try:
                target.relative_to(root.resolve())
                return False
            except (ValueError, OSError):
                continue
        return True


class RunArcpyCode(Tool):
    """在 arcpy 环境里执行一段 Python（默认关闭，需在配置里显式开启）。"""

    name = "run_arcpy_code"
    needs_project = True

    def run_arcpy_code(self, ctx: Context, code: str, timeout_seconds: Optional[float] = None) -> str:
        """审计保留模式禁止执行任意 Python 代码，避免绕过副本、写锁和操作历史。

        本工具仅为兼容 allow_arcpy_code 旧配置保留；启用配置也不能执行代码。
        请使用固定工具或已分类的 run_arcpy_tool。timeout_seconds 为兼容参数。
        """
        view = self.view(ctx)
        try:
            result = self.arcpy("run_arcpy_code", ctx=ctx, code=code,
                                project_root=str(view.root) if view.root else None,
                                timeout_seconds=timeout_seconds)
        except WorkerError as exc:
            return self.error("ArcPyError", exc)
        return self.dumps({"ok": True, **result})
