"""Explicit, audited entry for tools without a permanent adapter."""
from typing import Any, Dict, List, Optional

from ..core.mcp_compat import Context, ToolError
from ..core.tool import Tool


class RunUnadaptedTool(Tool):
    name = 'run_unadapted_tool'
    needs_project = True

    def run_unadapted_tool(self, ctx: Context, tool: str, reason: str,
                           parameters: Dict[str, Any], input_parameters: List[str],
                           output_parameter: Optional[str] = None,
                           edit_parameter: Optional[str] = None,
                           overwrite: bool = False, confirm: bool = False) -> str:
        """临时执行没有固定封装/适配器的 ArcPy 工具，必须说明使用理由。

        先 describe_arcpy_tool 检查真实参数。input_parameters 列出所有数据集输入参数名；
        output_parameter（单一显式输出）与 edit_parameter（原地编辑目标）必须二选一。
        所有数据集输入先复制，实际调用只收到副本；成功返回 outputs 和 primary_output。
        输入限项目内独立要素类/表/Shapefile；参数名大小写与 describe 一致。
        不支持栅格、多值数据集、工作空间修改、多输出及复杂关系数据。
        删除/清空等操作仍必须 confirm=true，不能借本入口擦除历史数据。
        仅支持 management/analysis/conversion/cartography/stats/edit 标准工具箱。
        这不是自定义工具沙箱；AI 必须确认工具语义及未声明副作用。
        理由、实际调用、阶段日志持久保存，用 job_logs 查询；lineage/explain 查成功链路。
        返回 pending/job_id 后用 job_result 查询，使用 primary_output 继续下一步。
        优先用固定封装或 run_arcpy_tool；不承诺所有未封装工具都可运行。
        """
        if not reason.strip():
            raise ToolError('reason 必须说明使用未封装工具的具体理由。')
        result = self.arcpy('run_unadapted_tool', ctx=ctx, tool=tool, reason=reason,
            parameters=parameters, input_parameters=input_parameters,
            output_parameter=output_parameter, edit_parameter=edit_parameter,
            overwrite=overwrite, confirm=confirm)
        return self.dumps({**self.project_block(self.view(ctx)), **result})
