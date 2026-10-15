"""Project-scoped processing history and asynchronous task queries."""
from __future__ import annotations

from ..core.history import History
from ..core.mcp_compat import Context, ToolError
from ..core.resolver import resolve_output_path
from ..core.tool import Tool


def project_history(view):
    if view.root is None:
        raise ToolError('审计查询必须先 use_project 指定项目根目录。')
    return History(view.root)


class Lineage(Tool):
    name = 'lineage'
    needs_project = True

    def lineage(self, ctx: Context, path: str, depth: int = 30,
                downstream: bool = False) -> str:
        """查询整个要素类/表/栅格的加工链路。path 使用返回的实际版本路径。

        默认回溯来源；downstream=true 查询下游影响面。保留每一步输入、输出、参数和时间。
        不追踪单个图斑，不记录 ArcGIS Pro 中的外部手工修改。
        """
        view = self.view(ctx)
        result = project_history(view).lineage(resolve_output_path(path, view.settings), depth, downstream)
        return self.dumps({'ok': True, **self.project_block(view), **result})


class Explain(Tool):
    name = 'explain'
    needs_project = True

    def explain(self, ctx: Context, path: str, depth: int = 30) -> str:
        """按时间解释一个数据集如何生成，列出每一步操作、参数及保留的中间数据路径。"""
        view = self.view(ctx)
        history = project_history(view)
        result = history.lineage(resolve_output_path(path, view.settings), depth)
        lines = []
        for i, step in enumerate(result['steps'], 1):
            payload = step['result'] or {}
            label = payload.get('operation_summary') or payload.get('tool') or step['args'].get('operation') or step['op']
            params = payload.get('params') or payload.get('parameters') or step['args'].get('params') or {}
            detail = '；参数：' + self.dumps(params) if params else ''
            lines.append(f"{i}. {label}：{', '.join(step['inputs']) or '新建'} → {', '.join(step['outputs'])}{detail}")
        result['explanation'] = '\n'.join(lines) if lines else '未找到 MCP 加工记录；该路径可能是外部原始数据。'
        return self.dumps({'ok': True, **self.project_block(view), **result})


class JobStatus(Tool):
    name = 'job_status'
    needs_project = True

    def job_status(self, ctx: Context, job_id: str) -> str:
        """查询当前项目任务状态；不等待 ArcPy worker，重启后仍可查。"""
        view = self.view(ctx)
        try:
            result = project_history(view).get(job_id)
        except ValueError as exc:
            return self.error('JobNotFound', exc, view)
        result.pop('result', None)
        return self.dumps({**self.project_block(view), **result})


class JobResult(Tool):
    name = 'job_result'
    needs_project = True

    def job_result(self, ctx: Context, job_id: str) -> str:
        """获取任务的持久化结果。succeeded 时用 result.output 或 result.outputs 继续下一步。

        queued/running 时继续轮询，不要重新提交。unknown 时检查保留成果与事务日志。
        """
        view = self.view(ctx)
        try:
            result = project_history(view).get(job_id)
        except ValueError as exc:
            return self.error('JobNotFound', exc, view)
        return self.dumps({**self.project_block(view), **result})
