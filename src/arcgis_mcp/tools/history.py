"""Project-scoped processing history and asynchronous task queries."""
from __future__ import annotations

import math
import time

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
                downstream: bool = False, detail: bool = False,
                offset: int = 0, limit: int = 20) -> str:
        """查询整个要素类/表/栅格的加工链路。path 使用返回的实际版本路径。

        默认回溯来源；downstream=true 查询下游影响面。保留每一步输入、输出、参数和时间。
        不追踪单个图斑，不记录 ArcGIS Pro 中的外部手工修改。
        默认精简为步骤、路径、参数和时间，不重复整个结果/schema；detail=true 获取完整步骤。
        offset/limit 分页，最多 100 步一页；next_offset 非空时继续查询。
        """
        view = self.view(ctx)
        result = project_history(view).lineage(resolve_output_path(path, view.settings), depth, downstream,
                                               detail, offset, limit)
        return self.dumps({'ok': True, **self.project_block(view), **result})


class Explain(Tool):
    name = 'explain'
    needs_project = True

    def explain(self, ctx: Context, path: str, depth: int = 30,
                offset: int = 0, limit: int = 20, detail: bool = False) -> str:
        """按时间解释数据集如何生成，列出操作、参数及中间路径，默认精简分页。

        next_offset 非空时继续；detail=true 附完整步骤结果。不评价业务正确性。
        """
        view = self.view(ctx)
        history = project_history(view)
        result = history.lineage(resolve_output_path(path, view.settings), depth,
                                 detail=detail, offset=offset, limit=limit)
        lines = []
        for i, step in enumerate(result['steps'], result['offset'] + 1):
            payload = step.get('result') or {}
            label = step.get('operation') or payload.get('operation_summary') or payload.get('tool') or step.get('args', {}).get('operation') or step.get('op')
            params = step.get('params') or payload.get('params') or payload.get('parameters') or step.get('args', {}).get('params') or {}
            detail = '；参数：' + self.dumps(params) if params else ''
            lines.append(f"{i}. {label}：{', '.join(step['inputs']) or '新建'} → {', '.join(step['outputs'])}{detail}")
        result['explanation'] = ('\n'.join(lines) if lines else '本页没有步骤。' if result['total_steps']
                                 else '未找到 MCP 加工记录；该路径可能是外部原始数据。')
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
        result.pop('args', None)
        return self.dumps({**self.project_block(view), **result})


class JobResult(Tool):
    name = 'job_result'
    needs_project = True

    def job_result(self, ctx: Context, job_id: str, wait_seconds: float = 0,
                   detail: bool = False) -> str:
        """获取任务的持久化结果。succeeded 时用 result.output 或 result.outputs 继续下一步。

        queued/running 时继续轮询，不要重新提交。unknown 时检查保留成果与事务日志。
        wait_seconds 为 0~5 秒的有界等待，不占用 ArcPy 执行锁；超时仍返回当前状态，不重放任务。
        默认省略重复的调用 args，字段 schema 仅返回 name/type；detail=true 返回完整原始结果。
        """
        view = self.view(ctx)
        if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 5:
            raise ToolError('wait_seconds 必须在 0~5 秒之间')
        try:
            history = project_history(view)
            deadline = time.monotonic() + wait_seconds
            while True:
                result = history.get(job_id)
                remaining = deadline - time.monotonic()
                if result['state'] not in ('queued', 'starting', 'running') or remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))
        except ValueError as exc:
            return self.error('JobNotFound', exc, view)
        if not detail:
            result.pop('args', None)
            if isinstance(result.get('result'), dict):
                result['result'] = self.compact_schema(result['result'])
        return self.dumps({**self.project_block(view), **result})
