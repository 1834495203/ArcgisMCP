"""Submit immutable script snapshots to the audited workflow runtime."""
from pathlib import Path
import json
import os
from typing import Any, Dict, List, Optional

from ..core.mcp_compat import Context, ToolError
from ..core.resolver import resolve_input_path, resolve_output_path
from ..core.tool import Tool
from ..core.workflow_plan import WorkflowPlan, export_script, graph_script, identifier, unit_script


class RunWorkflow(Tool):
    name = 'run_workflow'
    needs_project = True

    def run_workflow(self, ctx: Context, script_path: str, reason: str,
                     inputs: Dict[str, str], parameters: Optional[Dict[str, Any]] = None,
                     output_gdb: Optional[str] = None, timeout_seconds: int = 3600,
                     confirm: bool = False, input_mode: str = 'snapshot') -> str:
        """运行 AI 编写的 Python 工作流，保存提交时脚本快照、理由、参数及逐步链路。

        script_path 为项目内 .py 文件，必须 confirm=true。inputs 为别名到数据集路径；
        首版输入支持可保真复制的独立要素类/表/Shapefile，先复制后交给脚本。
        脚本使用注入的 workflow.inputs / workflow.parameters，通过 workflow.step 登记每步；
        step.output 提供暂存输出，step.edit_copy 在副本上编辑，退出步骤后用 step.result。
        workflow.set_outputs 指定最终成果。中间成功成果保留，不自动清理，失败不自动重放。
        已有数据布局时固定复用过程库；仅首次选择时可指定 output_gdb，遵守 auto_create_gdb。
        交互式实验/正式步骤优先使用 run_workflow_step，本入口用于批量脚本和导出流程重跑。
        timeout_seconds 10~86400 秒；提交后按 job_id 用 job_result/job_logs 查询。
        脚本在独立 ArcPy 进程运行。这是可信本机执行约定，不是 Python/文件系统沙箱。
        禁止硬编码原始数据写路径、绕过 SDK 写入或创建未登记副作用；未登记行为无法完整追踪。
        input_mode 默认 snapshot；read_only 原样引用输入，支持 ArcPy 可读的数据/工作空间，
        代码不得修改这些引用。强制保护必须部署执行账户只读权限；此选项本身不提供隔离。
        业务正确性由 AI/调用方验证。脚本示例见 docs/workflows.md。
        """
        if not confirm:
            raise ToolError('运行工作流脚本必须 confirm=true；运行器不是安全沙箱。')
        if not reason.strip():
            raise ToolError('必须填写工作流使用理由 reason。')
        if not 10 <= timeout_seconds <= 86400:
            raise ToolError('timeout_seconds 必须在 10~86400 秒之间。')
        if input_mode not in ('snapshot', 'read_only'):
            raise ToolError('input_mode 必须为 snapshot/read_only。')
        view = self.view(ctx)
        path = resolve_input_path(script_path, view.settings).resolve()
        try:
            path.relative_to(view.root)
        except ValueError:
            raise ToolError('脚本必须位于当前项目内。')
        if path.suffix.lower() != '.py' or not path.is_file():
            raise ToolError('script_path 必须指向存在的 .py 文件。')
        code = path.read_text(encoding='utf-8-sig')
        sources = {alias: str(resolve_input_path(value, view.settings)) for alias, value in inputs.items()}
        plan = WorkflowPlan(view.root)
        requested = resolve_output_path(output_gdb, view.settings) if output_gdb else None
        layout = plan.layout(process_gdb=str(requested or view.settings.output_workspace or Path(view.root) / 'work.gdb'))
        gdb = Path(layout['process'])
        if requested and requested.resolve() != gdb:
            raise ToolError('项目过程库已固定；不能为每次运行更换 GDB。请使用配置的 process 库。')
        if any(plan.role(p, layout) == 'temporary' for p in sources.values()):
            raise ToolError('正式运行不能读取实验临时成果，请从原始或正式过程数据重算。')
        result = self.arcpy('run_workflow', ctx=ctx, code=code, script_path=str(path),
            reason=reason, inputs=sources, parameters=parameters or {},
            output_gdb=str(gdb), timeout_seconds=timeout_seconds, confirm=confirm, layout=layout,
            input_mode=input_mode)
        return self.dumps({**self.project_block(view), **result})


class ConfigureWorkflowData(Tool):
    name = 'configure_workflow_data'
    needs_project = True

    def configure_workflow_data(self, ctx: Context, process_gdb: str = './process.gdb',
            temporary_gdb: str = './temporary.gdb', delivery_gdb: str = './delivery.gdb',
            original_roots: Optional[List[str]] = None) -> str:
        """一次性固定项目原始/过程/临时/交付数据布局，仅登记配置，不在此建库。

        三个输出 GDB 必须不同且位于项目内；开始执行前可修正，执行后不能按步骤更换，已有数据不搬迁。
        原始根仅登记说明，不能将其当输出库。不调用时单步入口使用默认三个库。
        每个角色至多一个 GDB，不为每步或实验重复新建库。受 auto_create_gdb 控制。
        """
        view = self.view(ctx)
        try:
            layout = WorkflowPlan(view.root).configure(process_gdb, temporary_gdb, delivery_gdb, original_roots)
        except ValueError as exc:
            raise ToolError(str(exc))
        return self.dumps({'ok': True, 'layout': layout, **self.project_block(view)})


class RunWorkflowStep(Tool):
    name = 'run_workflow_step'
    needs_project = True

    def run_workflow_step(self, ctx: Context, workflow_id: str, step_id: str,
            script_path: str, reason: str, inputs: Dict[str, str], outputs: Dict[str, str],
            description: str, input_descriptions: Dict[str, str], mode: str = 'draft', parameters: Optional[Dict[str, Any]] = None,
            edit_inputs: Optional[Dict[str, str]] = None, deliverables: Optional[List[str]] = None,
            timeout_seconds: int = 3600, confirm: bool = False, input_mode: str = 'snapshot') -> str:
        """增量执行一个脚本步骤，默认 draft 实验；final 成功步骤纳入可导出的正式流程。

        每步必须有 reason、description、命名 inputs/outputs 和逐项 input_descriptions；outputs 的值是成果说明，不是路径。
        脚本是步骤正文，使用 inputs/outputs/parameters/step，不写 workflow.step 外壳。
        outputs 中所有路径由服务端分配；edit_inputs 为 输出别名→输入别名，先复制再编辑。
        step.check(name, bool, details, required=True) 记录 AI 自行设计的验证，服务端不内置业务阈值。
        draft 的所有副本/输出进入 temporary 库；可 outputs={} 只分析，不能写交付区。
        final 输入不能来自临时区，必须从原始/正式成果重算；成功步骤 ID 不可重复覆盖。
        deliverables 指定交付输出别名，仅 final 可用，其余输出进入 process 库。
        数据布局用 configure_workflow_data 一次配置，不能每步创建或选择新 GDB。
        可交替实验和正式执行，用 workflow_steps 查询、export_workflow 导出正式脚本。
        必须 confirm=true；仍是可信本机代码约定，不能阻止脚本绕过 SDK 产生未登记副作用。
        inputs 可用 @上游step_id.输出别名 引用本流程成功节点，无须拼路径。
        input_mode=read_only 可直接读取栅格/工作空间等输入，但不复制、不提供权限隔离；
        原始引用禁止写入，部署只读权限才能强制保证。snapshot 保持原有保真复制检查。
        """
        if not confirm or not reason.strip() or not description.strip():
            raise ToolError('步骤需要 confirm=true、非空 reason 和 description。')
        if mode not in ('draft', 'final') or not 10 <= timeout_seconds <= 86400:
            raise ToolError('mode 必须是 draft/final，timeout_seconds 为 10~86400。')
        if input_mode not in ('snapshot', 'read_only'):
            raise ToolError('input_mode 必须为 snapshot/read_only。')
        view = self.view(ctx)
        try:
            identifier(workflow_id)
            identifier(step_id)
            for alias in list(inputs) + list(outputs):
                identifier(alias)
        except ValueError as exc:
            raise ToolError(str(exc))
        path = resolve_input_path(script_path, view.settings).resolve()
        try:
            path.relative_to(view.root)
        except ValueError:
            raise ToolError('步骤脚本必须在项目内。')
        if not path.is_file() or path.suffix.lower() != '.py':
            raise ToolError('步骤脚本须为存在的 .py 文件。')
        plan = WorkflowPlan(view.root)
        layout = plan.layout(process_gdb=str(view.settings.output_workspace) if view.settings.output_workspace else None)
        try:
            resolved = plan.resolve_references(workflow_id, inputs, mode)
        except ValueError as exc:
            raise ToolError(str(exc))
        spec = {'workflow_id': workflow_id, 'step_id': step_id, 'mode': mode,
            'description': description, 'reason': reason, 'code': path.read_text(encoding='utf-8-sig'),
            'input_descriptions': input_descriptions,
            'inputs': {k: str(resolve_input_path(p, view.settings)) for k, p in resolved.items()},
            'input_references': inputs, 'input_mode': input_mode,
            'outputs': outputs, 'parameters': parameters or {}, 'edit_inputs': edit_inputs or {},
            'deliverables': deliverables or []}
        result = self.arcpy('run_workflow', ctx=ctx, code=unit_script(spec), script_path=str(path),
            reason=reason, inputs=spec['inputs'], parameters=spec['parameters'],
            output_gdb=layout['temporary' if mode == 'draft' else 'process'],
            timeout_seconds=timeout_seconds, confirm=True, layout=layout, mode=mode, unit=spec,
            input_mode=input_mode)
        return self.dumps({**self.project_block(view), 'workflow_id': workflow_id,
                           'step_id': step_id, 'mode': mode, 'layout': layout, **result})


class WorkflowSteps(Tool):
    name = 'workflow_steps'
    needs_project = True

    def workflow_steps(self, ctx: Context, workflow_id: str, include_drafts: bool = False,
            detail: bool = False, offset: int = 0, limit: int = 20) -> str:
        """分页查询流程步骤的说明、理由、参数、输入输出角色与实际成果；默认只查正式步骤。

        include_drafts=true 查询实验；detail=true 附每步完整脚本。失败也可查，job_logs 取验证记录。
        """
        view = self.view(ctx)
        plan = WorkflowPlan(view.root)
        items = plan.units(workflow_id, include_drafts, detail)
        offset, limit = max(0, offset), max(1, min(100, limit))
        page = items[offset:offset + limit]
        return self.dumps({'ok': True, **self.project_block(view), 'workflow_id': workflow_id,
            'layout': plan.layout(create=False), 'steps': page, 'total': len(items),
            'next_offset': offset + len(page) if offset + len(page) < len(items) else None})


class SaveWorkflowNode(Tool):
    name = 'save_workflow_node'
    needs_project = True

    def save_workflow_node(self, ctx: Context, workflow_id: str, step_id: str,
            code: str, description: str, reason: str, inputs: Dict[str, str],
            input_descriptions: Dict[str, str], outputs: Dict[str, str],
            parameters: Optional[Dict[str, Any]] = None, edit_inputs: Optional[Dict[str, str]] = None,
            deliverables: Optional[List[str]] = None) -> str:
        """保存或更新工作流节点，不执行。code 为任意可信 ArcPy Python 步骤正文。

        inputs 是路径或 @节点ID.输出别名；允许先保存下游，运行前检查完整 DAG 与循环。
        outputs 为别名到说明，服务端按节点/输出别名命名实际成果；不需要手写过程路径。
        编辑通过 edit_inputs 指定输出到输入的副本映射；节点内可自由加工工作副本。
        使用 inputs/outputs/parameters/step；step.check 保存自行编写的验证。
        更新定义不修改历史执行快照；run_saved_workflow 运行整图，workflow_nodes 查询。
        """
        view = self.view(ctx)
        spec = dict(workflow_id=workflow_id, step_id=step_id, code=code, description=description,
            reason=reason, inputs={k: (p if p.startswith('@') else str(resolve_input_path(p, view.settings)))
                                   for k, p in inputs.items()},
            input_descriptions=input_descriptions, outputs=outputs, parameters=parameters or {},
            edit_inputs=edit_inputs or {}, deliverables=deliverables or [], mode='final')
        try:
            WorkflowPlan(view.root).save_node(spec)
        except (ValueError, SyntaxError) as exc:
            raise ToolError(str(exc))
        return self.dumps({'ok': True, 'workflow_id': workflow_id, 'step_id': step_id,
                           'note': '仅保存定义；执行前检查完整依赖，历史快照保持不变。'})


class WorkflowNodes(Tool):
    name = 'workflow_nodes'
    needs_project = True

    def workflow_nodes(self, ctx: Context, workflow_id: str, detail: bool = False,
                       offset: int = 0, limit: int = 20) -> str:
        """查询保存的工作流节点及输入引用、输出、说明、理由和参数；detail=true 返回代码。

        同时检查图结构；incomplete 的定义仍可继续编辑，不能直接运行。
        执行历史使用 workflow_steps/job_result 查询。
        """
        nodes = WorkflowPlan(self.view(ctx).root).nodes(workflow_id)
        try:
            _, _, _, order = graph_script(nodes)
            validation = {'valid': True, 'execution_order': order}
        except ValueError as exc:
            validation = {'valid': False, 'error': str(exc)}
        offset, limit = max(0, offset), max(1, min(100, limit))
        page = [{k: v for k, v in n.items() if detail or k != 'code'} for n in nodes[offset:offset + limit]]
        return self.dumps({'ok': True, 'workflow_id': workflow_id, 'nodes': page, 'total': len(nodes),
            'graph': validation, 'next_offset': offset + len(page) if offset + len(page) < len(nodes) else None})


class ListWorkflows(Tool):
    name = 'list_workflows'
    needs_project = True

    def list_workflows(self, ctx: Context, offset: int = 0, limit: int = 20) -> str:
        """列出项目中可编辑/运行的已保存工作流及节点数量；执行历史另查 workflow_steps。"""
        flows = WorkflowPlan(self.view(ctx).root).workflows()
        offset, limit = max(0, offset), max(1, min(100, limit))
        page = flows[offset:offset + limit]
        return self.dumps({'ok': True, 'workflows': page, 'total': len(flows),
            'next_offset': offset + len(page) if offset + len(page) < len(flows) else None})


class RunSavedWorkflow(Tool):
    name = 'run_saved_workflow'
    needs_project = True

    def run_saved_workflow(self, ctx: Context, workflow_id: str, reason: str,
            inputs: Optional[Dict[str, str]] = None, parameters: Optional[Dict[str, Dict[str, Any]]] = None,
            timeout_seconds: int = 3600, confirm: bool = False, input_mode: str = 'snapshot') -> str:
        """执行已保存 DAG，按依赖串行运行，节点引用自动绑定本轮新成果；返回 job_id。

        inputs 可覆盖外部输入，键为 节点ID__输入别名；parameters 按节点ID覆盖参数。
        快照包含本轮全部节点定义，后续修改不会影响已提交运行。失败停止，成功节点保留，未知不自动重放。
        input_mode=snapshot 默认保真副本；read_only 允许更广泛数据类型，直接只读引用。
        read_only 需要可信代码遵守只读约定，强制保护原件请配置执行账户权限。
        confirm=true 不是权限隔离；节点中未登记的代码副作用无法全面审计。
        """
        if not confirm or not reason.strip() or not 10 <= timeout_seconds <= 86400:
            raise ToolError('需要 confirm=true、非空理由和 10~86400 秒超时。')
        if input_mode not in ('snapshot', 'read_only'):
            raise ToolError('input_mode 必须为 snapshot/read_only。')
        view = self.view(ctx)
        plan = WorkflowPlan(view.root)
        nodes = plan.nodes(workflow_id)
        try:
            code, sources, defaults, order = graph_script(nodes)
        except (ValueError, SyntaxError) as exc:
            raise ToolError(str(exc))
        if set(inputs or {}) - set(sources) or set(parameters or {}) - set(defaults):
            raise ToolError('输入/参数覆盖包含未知节点或输入别名；用 workflow_nodes 查询。')
        sources.update(inputs or {})
        sources = {k: str(resolve_input_path(p, view.settings)) for k, p in sources.items()}
        layout = plan.layout(process_gdb=str(view.settings.output_workspace) if view.settings.output_workspace else None)
        if any(plan.role(p, layout) == 'temporary' or (plan.role(p, layout) in ('process', 'delivery')
                and not plan.confirmed_output(p)) for p in sources.values()):
            raise ToolError('流程外部输入不能是临时或未确认的过程成果。')
        result = self.arcpy('run_workflow', ctx=ctx, code=code, script_path='<saved:' + workflow_id + '>',
            reason=reason, inputs=sources, parameters={**defaults, **(parameters or {})},
            output_gdb=layout['process'], timeout_seconds=timeout_seconds, confirm=True, layout=layout,
            input_mode=input_mode, graph={'workflow_id': workflow_id, 'nodes': nodes, 'order': order})
        return self.dumps({'workflow_id': workflow_id, **self.project_block(view), **result})


class ExportWorkflow(Tool):
    name = 'export_workflow'
    needs_project = True

    def export_workflow(self, ctx: Context, workflow_id: str, source: str = 'executed') -> str:
        """导出成功正式步骤为可读、可运行 Python 脚本及 inputs/parameters/steps JSON。

        自动重连步骤依赖，重跑用新成果路径，不依赖原运行的过程名或临时数据。
        实验/失败步骤不纳入。文件写在项目 workflows/，已有导出加数字序号，保留旧版。
        用 run_workflow 或 python -m arcgis_mcp.workflow 执行，依赖同版本运行器和 ArcPy。
        source=executed 导出成功正式历史；definition 导出当前已保存图，不表示已经验证通过。
        """
        view = self.view(ctx)
        plan = WorkflowPlan(view.root)
        units = plan.units(workflow_id, detail=True)
        try:
            if source == 'definition':
                units = plan.nodes(workflow_id)
                code, sources, params, _ = graph_script(units)
            elif source == 'executed':
                # A saved graph may have many runs. Do not mix the same node from different runs.
                graph_runs = [u.get('parent_job_id') for u in units if u.get('parent_job_id')]
                latest = plan.latest_graph_run(workflow_id)
                if latest or graph_runs:
                    if latest and latest['state'] in ('queued', 'starting', 'running', 'unknown'):
                        raise ValueError('最近流程运行尚未结束或结果未知，不能导出为成功执行流程。')
                    parent_id = latest['job_id'] if latest else graph_runs[-1]
                    units = [u for u in units if u.get('parent_job_id') == parent_id]
                    if any(u.get('parent_state') in ('queued', 'starting', 'running', 'unknown') for u in units):
                        raise ValueError('最近流程运行尚未结束或结果未知，不能导出为成功执行流程。')
                units = [u for u in units if u['state'] == 'succeeded']
                code, sources, params = export_script(units)
            else:
                raise ValueError('source 必须是 executed/definition。')
        except ValueError as exc:
            raise ToolError(str(exc))
        directory = Path(view.root) / 'workflows'
        directory.mkdir(exist_ok=True)
        try:
            directory.resolve().relative_to(Path(view.root).resolve())
        except ValueError:
            raise ToolError('导出目录不能指向项目之外。')
        number = 1
        while True:
            stem = workflow_id + ('' if number == 1 else '_' + str(number))
            script = directory / (stem + '.py')
            pending = directory / (stem + '.exporting')
            companions = [directory / (stem + '.' + suffix + '.json') for suffix in ('inputs', 'parameters', 'steps')]
            if script.exists() or any(p.exists() for p in companions):
                number += 1
                continue
            try:
                # Reserve the basename; publish runnable source only after all companion files.
                compile(code, str(script), 'exec')
                with pending.open('x', encoding='utf-8', newline='\n') as stream:
                    stream.write(code)
                    stream.flush()
                    os.fsync(stream.fileno())
                break
            except FileExistsError:
                number += 1
        paths = {'script': str(script)}
        for suffix, payload in [('inputs', sources), ('parameters', params), ('steps', units)]:
            target = directory / (stem + '.' + suffix + '.json')
            with target.open('x', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            paths[suffix] = str(target)
        if script.exists():
            raise ToolError('导出目标在写入期间被创建，暂存导出保留，拒绝覆盖。')
        os.replace(str(pending), str(script))
        return self.dumps({'ok': True, **self.project_block(view), 'files': paths, 'source': source,
            'input_mode': 'read_only' if any(u.get('input_mode') == 'read_only' for u in units) else 'snapshot',
            'layout': plan.layout(), 'note': '使用输入/参数 JSON 配合 run_workflow 或 CLI 重跑；不承诺跨版本数值完全一致。'})
