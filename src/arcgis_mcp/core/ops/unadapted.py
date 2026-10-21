"""Declaration-bound ArcPy calls. Worker-only, Python 3.9 compatible.

Metadata and declarations are an execution contract, not a sandbox for malicious
toolboxes. Only installed Esri toolbox tools and standalone vector/table inputs.
"""
import os
from contextlib import ExitStack
from pathlib import Path

from . import safety
from .common import WorkerError, ensure_output_container, path_exists, path_key


def dataset_type(parameter):
    value = ''.join(str(parameter.datatype).lower().split())
    return any(token in value for token in (
        'featurelayer', 'featureclass', 'tableview', 'detable',
        '要素图层', '要素类', '表视图')) or value in ('table', '表')


def scalar_type(parameter):
    value = ''.join(str(parameter.datatype).lower().split())
    return value in ('gpstring', 'string', 'gpboolean', 'boolean', 'gplong', 'long',
        'gpdouble', 'double', 'gpfield', 'field', 'gplinearunit', 'linearunit',
        'gparealunit', 'arealunit', 'gpdate', 'date', '字符串', '布尔型',
        '长整型', '双精度', '字段', '线性单位', '面积单位', '日期')


def run_unadapted_tool(tool, parameters, reason, input_parameters,
                       output_parameter=None, edit_parameter=None, overwrite=False, confirm=False):
    import arcpy
    from history import History
    from .meta import resolve_tool_name, _is_destructive

    if not safety.CURRENT_ROOT or not safety.CURRENT_JOB:
        raise WorkerError('未封装调用必须经过持久化任务与审计入口。')
    history = History(safety.CURRENT_ROOT)
    job = safety.CURRENT_JOB
    history.event(job, 'requested', {'tool': tool, 'reason': reason, 'parameters': parameters,
        'input_parameters': input_parameters, 'output_parameter': output_parameter,
        'edit_parameter': edit_parameter})
    try:
        if not isinstance(reason, str) or not reason.strip():
            raise WorkerError('必须填写使用未封装工具的具体理由 reason。')
        if not isinstance(parameters, dict) or not parameters:
            raise WorkerError('parameters 必须是非空参数对象。')
        if not input_parameters or len(set(input_parameters)) != len(input_parameters):
            raise WorkerError('必须声明不重复的 input_parameters。')
        if bool(output_parameter) == bool(edit_parameter):
            raise WorkerError('必须且只能指定 output_parameter 或 edit_parameter。')
        name = resolve_tool_name(tool)
        if _is_destructive(name) and not confirm:
            raise WorkerError('删除/清空等操作即使在副本上执行，也必须 confirm=true。')
        # Do not accept arbitrary Python attributes or custom toolbox aliases.
        if name not in arcpy.ListTools('*') or name.rsplit('_', 1)[-1].lower() not in (
                'management', 'analysis', 'conversion', 'cartography', 'stats', 'edit'):
            raise WorkerError('此入口仅支持已安装的指定 Esri 标准工具箱，不执行自定义 Python 工具。')
        metadata = {p.name: p for p in arcpy.GetParameterInfo(name)}
        if set(parameters) - set(metadata):
            raise WorkerError('未知参数：' + ', '.join(sorted(set(parameters) - set(metadata))))
        declared = set(input_parameters)
        if not declared <= set(parameters) or (edit_parameter and edit_parameter not in declared):
            raise WorkerError('输入及编辑目标必须是 parameters 中显式提供的输入参数名。')
        explicit_outputs = {k for k, p in metadata.items()
            if str(p.direction).lower() == 'output' and str(p.parameterType).lower() != 'derived'}
        if explicit_outputs != ({output_parameter} if output_parameter else set()):
            raise WorkerError('工具必须只有声明的单一显式输出；多输出/隐式输出需专门适配器。')
        root = Path(safety.CURRENT_ROOT).resolve()

        def resolve(value, output=False):
            if not isinstance(value, str) or not value.strip() or ';' in value:
                raise WorkerError('数据集参数仅支持单个路径字符串；多值/值表需专门适配器。')
            path = Path(value)
            if not path.is_absolute():
                base = Path(safety.OUTPUT_WORKSPACE) if safety.OUTPUT_WORKSPACE and len(path.parts) == 1 else root
                path = base / path
            path = path.resolve()
            try:
                path.relative_to(root)
            except ValueError:
                raise WorkerError('未封装调用的数据路径必须在当前项目内：' + str(path))
            if not output and not path_exists(str(path)):
                raise WorkerError('输入数据集不存在：' + str(path))
            return str(path)

        logical = dict(parameters)
        sources = {}
        for key, value in parameters.items():
            p = metadata[key]
            if key == output_parameter:
                if not dataset_type(p):
                    raise WorkerError('当前仅支持要素类/表的输出。')
                logical[key] = resolve(value, True)
                if Path(logical[key]).suffix.lower() not in ('', '.shp', '.dbf'):
                    raise WorkerError('临时入口输出限 GDB 要素类/表或 SHP/DBF；CSV 请用已适配的表格工具。')
            elif dataset_type(p):
                if key not in declared or str(p.direction).lower() != 'input' or p.multiValue:
                    raise WorkerError('所有数据集输入必须显式声明；不支持多值输入：' + key)
                sources[key] = resolve(value)
                logical[key] = sources[key]
            elif key in declared:
                raise WorkerError('声明的输入不是支持的数据集参数：' + key)
            else:
                kind = str(p.datatype).lower()
                if any(t in kind for t in ('workspace', 'folder', 'file', 'raster', 'valuetable',
                    'dataelement', '工作空间', '文件', '栅格', '值表', '数据元素')):
                    raise WorkerError('不支持工作空间/文件/栅格/值表副作用参数：' + key)
                if not scalar_type(p):
                    raise WorkerError('此参数类型尚未适配，不能当作普通值绕过输入审计：' + key)
                if not scalar(value):
                    raise WorkerError('普通参数仅支持标量或标量数组：' + key)
                values = value if isinstance(value, list) else [value]
                if any(isinstance(v, str) and (os.path.isabs(v) or v.startswith(('./', '../', '.\\'))
                    or '.gdb' in v.lower()) for v in values):
                    raise WorkerError('普通参数中包含未声明路径，需要专门适配：' + key)
        if set(sources) != declared:
            raise WorkerError('input_parameters 必须完整对应所有数据集输入。')
        if output_parameter and any(path_key(logical[output_parameter]) == path_key(p) for p in sources.values()):
            raise WorkerError('输出不能等于任何输入。')
        history.event(job, 'validated', {'tool': name, 'reason': reason.strip(), 'parameters': logical})
        params = dict(logical)
        copies = {}
        publications = []
        with ExitStack() as locks:
            targets = list(sources.values()) + ([logical[output_parameter]] if output_parameter else [])
            for ws in sorted({safety.workspace(p) for p in targets}, key=str.lower):
                locks.enter_context(safety.WorkspaceLease(os.path.join(ws, '_lease_target')))
            # Every dataset operand is private, even if the caller calls it read-only.
            for key, source in sources.items():
                if source not in copies:
                    final = safety.version_path(source, name.rsplit('_', 1)[0])
                    publication = safety.Publication(final, root=safety.CURRENT_ROOT)
                    publications.append(publication)
                    history.event(job, 'copy_started', {'source': source, 'work': publication.work, 'output': final})
                    safety.copy_for_edit(source, publication.work)
                    copies[source] = publication
                    history.event(job, 'copied', {'source': source, 'work': publication.work})
                params[key] = copies[source].work
            if output_parameter:
                primary = safety.Publication(logical[output_parameter], overwrite, safety.CURRENT_ROOT)
                ensure_output_container(primary.work)
                publications.append(primary)
                params[output_parameter] = primary.work
            else:
                primary = copies[sources[edit_parameter]]
            if len({path_key(p.final) for p in publications}) != len(publications):
                raise WorkerError('显式输出与副本发布路径冲突，请更换输出名。')
            invocation = 'arcpy.' + name + '(**' + repr(params) + ')'
            history.event(job, 'executing', {'tool': name, 'invocation': invocation,
                'bindings': params, 'reason': reason.strip()})
            try:
                with arcpy.EnvManager(overwriteOutput=False,
                        workspace=os.path.dirname(primary.work), scratchWorkspace=os.path.dirname(primary.work)):
                    result = getattr(arcpy, name)(**params)
                messages = str(result.getMessages()) if hasattr(result, 'getMessages') else str(arcpy.GetMessages())
                history.event(job, 'executed', {'messages': messages})
            except Exception:
                history.event(job, 'arcpy_error', {'messages': str(arcpy.GetMessages())})
                raise
            if any(not path_exists(p.work) for p in publications):
                raise WorkerError('工具移除输入副本或未生成声明输出；暂存保留，不发布。')
            outputs = []
            for publication in publications:
                history.event(job, 'publishing', {'work': publication.work, 'output': publication.requested})
                final = publication.publish()
                outputs.append({'path': final, 'exists': True})
                history.event(job, 'published', {'output': final})
        return {'tool': name, 'reason': reason.strip(), 'parameters': logical,
            'inputs': list(dict.fromkeys(sources.values())), 'outputs': outputs,
            'primary_output': primary.requested, 'source_modified': False,
            'input_copies': [{'source': source, 'output': p.final} for source, p in copies.items()],
            'retained_inputs': True, 'messages': messages,
            'archived_outputs': [a for p in publications for a in p.archives],
            'note': '未封装工具按声明执行；日志不构成业务正确性或所有副作用的验证证书。'}
    except Exception as exc:
        history.event(job, 'failed', {'error': str(exc), 'type': type(exc).__name__})
        raise


def scalar(value):
    return value is None or isinstance(value, (str, bool, int, float)) or (
        isinstance(value, list) and all(v is None or isinstance(v, (str, bool, int, float)) for v in value))
