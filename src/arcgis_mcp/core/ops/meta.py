"""搜索本机 ArcPy 目录、查看参数，仅执行已完成安全适配的工具。

目录数量随安装和扩展变化，不代表可执行数量或验证覆盖率。
执行和能力查询共用安全分类；路径与参数解析在后台 worker 内完成。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .common import (
    WorkerError,
    delete_dataset,
    describe_dataset,
    ensure_output_container,
    jsonable,
    path_exists,
    path_key,
)

#: 名字以这些开头的工具会直接删/清空数据，通用调用时必须显式 confirm
DESTRUCTIVE_PREFIXES = ("delete", "truncate", "drop")
#: 名字里含这些片段的也算破坏性（RemoveFeatureClassFromTopology 之类）
DESTRUCTIVE_FRAGMENTS = ("remove",)
#: 另外这些是原地改数据、也归入需要 confirm 的一类
DESTRUCTIVE_EXTRA = {"alterfield", "repairgeometry"}


def _is_destructive(name: str) -> bool:
    base = name.rsplit("_", 1)[0].lower()
    return (base.startswith(DESTRUCTIVE_PREFIXES)
            or any(fragment in base for fragment in DESTRUCTIVE_FRAGMENTS)
            or base in DESTRUCTIVE_EXTRA)


def resolve_tool_name(tool: str) -> str:
    """把用户给的工具名解析成 arcpy 里的实际名字（flat 名，例如 Buffer_analysis）。

    接受：Buffer、Buffer_analysis、analysis.Buffer、buffer（大小写不敏感）。
    """
    import arcpy

    raw = str(tool or "").strip()
    if not raw:
        raise WorkerError("tool（工具名）不能为空")
    # 支持「工具箱.工具名」写法（arcpy 里实际是 工具名_工具箱）
    if "." in raw:
        toolbox, _, tool_name = raw.rpartition(".")
        if toolbox and tool_name:
            raw = f"{tool_name}_{toolbox}"
    if hasattr(arcpy, raw):
        return raw

    all_tools = arcpy.ListTools("*")
    lower = {name.lower(): name for name in all_tools}
    if raw.lower() in lower:
        return lower[raw.lower()]

    # 只给了工具名没给工具箱：在 <Name>_<toolbox> 里找唯一匹配
    candidates = [name for name in all_tools if name.rsplit("_", 1)[0].lower() == raw.lower()]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise WorkerError(
            f"工具名 {tool!r} 对应多个实现，请写全（Name_toolbox 或 工具箱.工具名）：\n"
            + "\n".join(f"- {c}" for c in candidates[:15])
        )
    import difflib

    near = difflib.get_close_matches(raw, all_tools, n=8, cutoff=0.6)
    raise WorkerError(
        f"找不到工具 {tool!r}。" + (f"相近的有：{'、'.join(near)}" if near else "")
        + "\n先用 list_arcpy_tools 用英文关键词搜一下（例如 Dissolve / Tessellation / Topology）"
    )


def _tool_syntax(name: str) -> str:
    """取工具的语法行。

    实测：这个安装的 arcpy.Usage 只返回语法行（没有文字说明），
    但语法行本身很有用 —— 它把参数名和内联的可选值都列出来了。
    """
    import arcpy

    with contextlib.suppress(Exception):
        usage = (arcpy.Usage(name) or "").strip()
        first = usage.splitlines()[0].strip() if usage else ""
        return first[:220]
    return ""


def op_list_arcpy_tools(keyword: Optional[str] = None, toolbox: Optional[str] = None,
                        limit: int = 60, with_syntax: bool = True) -> Dict[str, Any]:
    """搜索本机工具目录，并标明安全适配状态；目录数不代表可执行数量。"""
    import arcpy

    names = sorted(arcpy.ListTools(f"*{keyword}*" if keyword else "*"))
    if toolbox:
        suffix = "_" + str(toolbox).strip().lower().lstrip("_")
        names = [n for n in names if n.lower().endswith(suffix)]

    limit = max(1, int(limit or 60))
    items: List[Dict[str, Any]] = []
    for name in names[:limit]:
        item: Dict[str, Any] = {"name": name, "toolbox": name.rsplit("_", 1)[-1]}
        item["execution"] = tool_execution_support(name)
        if with_syntax:
            syntax = _tool_syntax(name)
            if syntax:
                item["syntax"] = syntax
        items.append(item)

    result: Dict[str, Any] = {
        "keyword": keyword,
        "toolbox": toolbox,
        "total_matched": len(names),
        "classified_matched": sum(tool_execution_support(n)["allowed"] for n in names),
        "returned": len(items),
        "tools": items,
        "catalog_note": "total_matched 是本机匹配目录数，不是可执行或已验证工具数；优先使用固定封装。",
        "next_step": "只对 execution.allowed=true 的工具查询 describe_arcpy_tool；确认执行限制和参数后再调用 run_arcpy_tool。未适配工具不能执行，confirm=true 也不能绕过。",
    }
    if len(names) > limit:
        result["truncated"] = f"只返回前 {limit} 个；用更具体的关键词 / toolbox / limit 收窄"
    if not names:
        result["hint"] = ("没搜到。arcpy 工具名是英文，试试 Dissolve、Tessellation、Topology、"
                          "Interpolate、Raster 这类关键词；不带关键词可以看总量")
    return result


def op_describe_arcpy_tool(tool: str, include_usage: bool = True) -> Dict[str, Any]:
    """查看任意 arcpy 工具的用法与参数（方向、必填、数据类型、可选值）。"""
    import arcpy

    name = resolve_tool_name(tool)
    params: List[Dict[str, Any]] = []
    for p in arcpy.GetParameterInfo(name):
        allowed = getattr(getattr(p, "filter", None), "list", None) or []
        param: Dict[str, Any] = {
            "name": p.name,
            "direction": p.direction,                                    # Input / Output
            "required": str(p.parameterType).lower() == "required",
            'parameter_type': str(p.parameterType),
            "datatype": p.datatype,
            "multi_value": bool(p.multiValue),
        }
        if allowed:
            param["allowed_values"] = [jsonable(v) for v in allowed]
        if getattr(p, "parameterDependencies", None):
            param["depends_on"] = list(p.parameterDependencies)
        params.append(param)

    result: Dict[str, Any] = {
        "tool": tool,
        "resolved_name": name,
        "toolbox": name.rsplit("_", 1)[-1],
        "parameters": params,
        "execution": tool_execution_support(name, params),
        "input_parameters": [p["name"] for p in params if p["direction"] == "Input"],
        "output_parameters": [p["name"] for p in params if p["direction"] == "Output"],
        "usage_note": ("参数名用 arcpy 的英文名（in_features / out_feature_class …）；"
                       "查询参数不代表允许执行，先检查 execution.allowed 和 constraints。带 allowed_values 的"
                       "参数请从这些值里选（当前是中文版 ArcGIS，取值可能是中文关键字，"
                       "arcpy 也接受对应的英文关键字）。"),
    }
    if include_usage:
        with contextlib.suppress(Exception):
            usage = (arcpy.Usage(name) or "").strip()
            if usage:
                result["usage"] = usage[:4000]
    return result


def _flatten(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, str)]
    return []


# A name/direction alone does not prove a tool leaves inputs unchanged.
# Unknown tools fail closed until classified; derived outputs never authorize deletion.
SAFE_PRODUCERS = {
    'buffer_analysis', 'pairwisebuffer_analysis', 'intersect_analysis',
    'pairwiseintersect_analysis', 'union_analysis', 'erase_analysis', 'pairwiseerase_analysis',
    'clip_analysis', 'pairwiseclip_analysis', 'identity_analysis', 'spatialjoin_analysis',
    'select_analysis', 'frequency_analysis', 'statistics_analysis',
    'copyfeatures_management', 'copyrows_management',
    'merge_management', 'project_management', 'checkgeometry_management',
    'featuretopoint_management', 'featuretoline_management', 'multiparttosinglepart_management',
    'dissolve_management', 'pairwisedissolve_analysis', 'eliminate_management',
    'sort_management', 'minimumboundinggeometry_management',
    'smoothpolygon_cartography', 'smoothline_cartography',
    'rastertopolygon_conversion', 'rastertopoint_conversion', 'polygontoraster_conversion',
    'copyraster_management', 'resample_management', 'projectraster_management',
    'tabletoexcel_conversion', 'exceltotable_conversion', 'featurestojson_conversion',
    'jsontofeatures_conversion', 'xytabletopoint_management',
    'generatetessellation_management',
    'generatepointsalonglines_management',
}


EDIT_ADAPTERS = {
    'deletefeatures_management': ('delete_features', 'in_features', {}),
    'deleterows_management': ('delete_features', 'in_rows', {}),
    'addfield_management': ('add_field', 'in_table', {'field_name': 'field', 'field_type': 'field_type',
        'field_length': 'field_length', 'field_alias': 'field_alias', 'field_is_nullable': 'field_is_nullable'}),
    'deletefield_management': ('delete_field', 'in_table', {'drop_field': 'field'}),
    'alterfield_management': ('alter_field', 'in_table', {'field': 'field', 'new_field_name': 'new_name',
        'new_field_alias': 'new_alias', 'field_type': 'field_type', 'field_length': 'field_length'}),
    'addxy_management': ('add_xy_coordinates', 'in_features', {}),
    'addgeometryattributes_management': ('add_geometry_attributes', 'input_features', {
        'geometry_properties': 'geometry_properties', 'length_unit': 'length_unit',
        'area_unit': 'area_unit', 'coordinate_system': 'coordinate_system'}),
    'repairgeometry_management': ('repair_geometry', 'in_features', {
        'delete_null': 'delete_null', 'validation_method': 'validation_method'}),
    'calculatefield_management': ('calculate_fields', 'in_table', {
        'expression_type': 'expression_type', 'code_block': 'code_block', 'field_type': 'field_type'}),
    'append_management': ('append', 'target', {'inputs': 'in_features', 'schema_type': 'schema_type',
        'field_mapping': 'field_mapping', 'subtype': 'subtype', 'expression': 'expression',
        'match_fields': 'match_fields'}),
}

CREATE_ADAPTERS = {
    "createfeatureclass_management", "createtable_management",
    "createfilegdb_management", "tabletotable_conversion",
}
READ_ADAPTERS = {"getcount_management"}


def tool_execution_support(name: str, parameters=None) -> Dict[str, Any]:
    """Report adapter eligibility, not a promise that a particular invocation will run."""
    lower = name.lower()
    constraints = []
    if lower == 'delete_management':
        mode = 'archive_delete'
        constraints = ['需要 confirm=true；归档原件并移除原路径，不物理擦除历史成果。']
    elif lower in EDIT_ADAPTERS:
        mode = 'copy_edit'
        constraints = ['先复制再编辑；源数据保留，后续步骤使用返回的 output。',
                       '仅支持适配器映射的参数；数据属性无法保真时拒绝复制。']
        if lower in ('deletefeatures_management', 'deleterows_management'):
            constraints.append('删除记录需要 confirm=true。')
    elif lower in CREATE_ADAPTERS:
        mode = 'create_publish'
        constraints = ['out_path/out_folder_path 与 out_name 组合输出，暂存成功后发布。']
    elif lower in READ_ADAPTERS:
        mode = 'read'
    elif lower in SAFE_PRODUCERS:
        mode = 'produce_publish'
        constraints = ['仅支持单个显式输出；输出不能与输入相同，不能隐式修改输入。']
    else:
        mode = 'unsupported'
        constraints = ['尚未完成安全适配，拒绝执行；confirm=true 不能绕过，需添加安全适配器。']
    allowed = mode != 'unsupported'
    if allowed and mode != 'read':
        constraints.append('写操作返回 job_id；通过 job_status/job_result 获取状态及最终结果。')
    if allowed and mode in ('produce_publish', 'create_publish'):
        constraints.append('overwrite=true 归档旧目标后替换指定路径；写到项目/搜索根之外需要 confirm=true。')
    result = {
        'allowed': allowed,
        'status': 'adapter_available' if allowed else 'unsupported',
        'mode': mode,
        'constraints': constraints,
        'validation': 'not_certified_for_all_parameters',
        'reliability_note': '安全分类不代表所有参数组合均已实测；许可证、数据格式、坐标系、单位和结果仍需核验。',
    }
    if lower in EDIT_ADAPTERS:
        operation, target, mapping = EDIT_ADAPTERS[lower]
        supported = {target, *mapping}
        if operation == 'calculate_fields':
            supported.update(('field', 'expression'))
        result['supported_parameters'] = sorted(supported)
    if parameters is not None and mode == 'produce_publish':
        result['explicit_output_parameters'] = [p['name'] for p in parameters
            if str(p['direction']).lower() == 'output'
            and str(p.get('parameter_type', '')).lower() != 'derived']
    return result


def op_run_arcpy_tool(tool: str, parameters=None, output_parameters=None,
                      overwrite=False, confirm=False, allowed_roots=None):
    import arcpy
    from . import attributes, safety
    from .tabular import table_output
    from contextlib import ExitStack

    if not isinstance(parameters, dict) or not parameters:
        raise WorkerError('parameters 不能为空')
    name = resolve_tool_name(tool)
    lower = name.lower()
    if not tool_execution_support(name)['allowed']:
        raise WorkerError('该通用工具尚未通过输入不变/输出发布安全分类，已拒绝执行：' + name +
                          '。请使用已适配的工具，或为此工具添加安全适配器；confirm=true 不能绕过。')
    params = dict(parameters)
    # Normalize keys using ArcPy metadata, not English parameter name heuristics.
    metadata = {p.name.lower(): p for p in arcpy.GetParameterInfo(name)}
    unknown = [k for k in params if k.lower() not in metadata]
    if unknown:
        raise WorkerError('未知参数：' + str(unknown))
    params = {metadata[k.lower()].name: v for k, v in params.items()}
    root = safety.CURRENT_ROOT
    def resolve_value(value, parameter):
        if isinstance(value, (list, tuple)):
            return [resolve_value(v, parameter) for v in value]
        if not isinstance(value, str) or not value or not root:
            return value
        datatype = str(parameter.datatype).lower()
        is_path = any(token in datatype for token in ('feature', 'table', 'raster', 'workspace',
            'folder', 'file', 'extent', 'dataelement', 'data element', 'valuetable', 'value table',
            '要素', '图层', '栅格', '文件', '工作空间', '数据元素', '值表'))
        if not is_path or parameter.name.lower() == 'out_name':
            return value
        if ';' in value and getattr(parameter, 'multiValue', False):
            return [resolve_value(v.strip().strip("'\""), parameter) for v in value.split(';')]
        if os.path.isabs(value):
            return os.path.normpath(value)
        container = any(t in datatype for t in ('workspace', 'folder', '工作空间', '文件夹'))
        if (container or '/' in value or '\\' in value or Path(value).suffix or
                os.path.exists(os.path.join(root, value))):
            return os.path.normpath(os.path.join(root, value))
        return value
    params = {key: resolve_value(value, metadata[key.lower()]) for key, value in params.items()}
    lookup = {k.lower(): v for k, v in params.items()}
    logical_parameters = dict(params)
    def check_output(path):
        if confirm or not allowed_roots:
            return
        target = os.path.normcase(os.path.realpath(path))
        for folder in allowed_roots:
            base = os.path.normcase(os.path.realpath(folder))
            try:
                if os.path.commonpath([target, base]) == base:
                    return
            except ValueError:
                continue
        raise WorkerError('输出位于项目和搜索根之外，需要 confirm=true：' + path)
    if lower == 'delete_management':
        source = lookup.get('in_data')
        if not confirm:
            raise WorkerError('删除数据集需要 confirm=true，将归档原件并移除原路径。')
        if not source or not path_exists(source):
            raise WorkerError('删除目标不存在：' + str(source))
        with safety.WorkspaceLease(source):
            publication = safety.Publication(source, overwrite=True, root=root)
            output = publication.retire()
        return {'tool': name, 'parameters': logical_parameters, 'source': source, 'in_features': source,
                'output': output, 'inputs': [output], 'logical_deleted': True,
                'original_path_exists': False, 'source_modified': True, 'modified_in_place': False,
                **publication.fields(), 'target_replaced': False}
    if lower.startswith(('delete_', 'truncate', 'drop', 'remove')):
        raise WorkerError('审计保留模式拒绝直接删除历史成果（confirm=true 也不能绕过）；'
                          '请使用 delete_dataset(confirm=true) 创建保留副本。')
    if lower in CREATE_ADAPTERS:
        directory_key = 'out_folder_path' if lower == 'createfilegdb_management' else 'out_path'
        directory, leaf = lookup.get(directory_key), lookup.get('out_name')
        if not directory or not os.path.isabs(directory) or not leaf or Path(leaf).name != leaf:
            raise WorkerError('创建工具需要绝对目录和不含路径的 out_name')
        output = str(Path(directory) / leaf)
        check_output(output)
        if lower == 'createfilegdb_management' and not output.lower().endswith('.gdb'):
            output += '.gdb'
        with safety.WorkspaceLease(output):
            publication = safety.Publication(output, overwrite, safety.CURRENT_ROOT)
            if lower != 'createfilegdb_management':
                ensure_output_container(publication.work)
            else:
                Path(publication.work).parent.mkdir(parents=True, exist_ok=True)
            output_context = table_output(publication.work) if lower == 'tabletotable_conversion' else contextlib.nullcontext(publication.work)
            with output_context as actual:
                params[metadata[directory_key].name] = str(Path(actual).parent)
                params[metadata['out_name'].name] = Path(actual).name
                with arcpy.EnvManager(overwriteOutput=False):
                    getattr(arcpy, name)(**params)
            output = publication.publish()
        inputs = [p for value in (lookup.get('template'), lookup.get('in_rows'))
                  for p in _flatten(value) if path_exists(p)]
        entry = {'path': output, 'exists': True}
        with contextlib.suppress(Exception):
            entry.update(describe_dataset(output))
        return {'tool': name, 'parameters': logical_parameters, 'inputs': inputs,
                'outputs': [entry], 'retained_inputs': True, **publication.fields()}
    if lower in EDIT_ADAPTERS:
        operation, target_key, mapping = EDIT_ADAPTERS[lower]
        supported = {target_key, *mapping}
        if operation == 'calculate_fields':
            supported.update(('field', 'expression'))
        unsupported = set(lookup) - supported
        if unsupported:
            raise WorkerError('安全编辑适配器暂不支持这些参数：' + str(sorted(unsupported)))
        source = lookup.get(target_key)
        if not source or not path_exists(source):
            raise WorkerError('编辑输入不存在：' + str(source))
        edit_params = {dest: lookup[key] for key, dest in mapping.items() if key in lookup}
        if operation == 'delete_features':
            if not confirm:
                raise WorkerError('删除记录需要 confirm=true；仅在副本中删除。')
            edit_params['confirm'] = confirm
        if operation == 'calculate_fields':
            edit_params['assignments'] = {lookup['field']: lookup['expression']}
        if operation == 'repair_geometry' and 'delete_null' in edit_params:
            edit_params['delete_null'] = str(edit_params['delete_null']).upper() == 'DELETE_NULL'
        if isinstance(edit_params.get('in_features'), str):
            edit_params['in_features'] = [edit_params['in_features']]
        with safety.WorkspaceLease(source):
            publication = safety.Publication(safety.version_path(source, operation), root=safety.CURRENT_ROOT)
            safety.copy_for_edit(source, publication.work)
            with arcpy.EnvManager(overwriteOutput=False):
                result = attributes.OPERATIONS[operation]['run'](publication.work, edit_params) or {}
            output = publication.publish()
        result.update(tool=name, operation=operation, parameters=logical_parameters, params=edit_params,
                      in_features=source, output=output, source=source, modified_in_place=False, source_modified=False,
                      inputs=[source] + edit_params.get('in_features', []), retained_inputs=True)
        return result
    if lower in READ_ADAPTERS:
        return {'tool': name, 'parameters': params, 'value': str(getattr(arcpy, name)(**params)[0]),
                'inputs': [], 'outputs': []}
    if lower not in SAFE_PRODUCERS:
        raise WorkerError('该通用工具尚未通过输入不变/输出发布安全分类，已拒绝执行：' + name +
                          '。请使用对应固定工具，或为此工具添加安全适配器。')
    out_keys = [key for key, p in metadata.items()
                if str(p.direction).lower() == 'output' and str(p.parameterType).lower() != 'derived'
                and key in lookup and lookup[key] not in (None, '')]
    if not out_keys:
        raise WorkerError('未识别到显式输出数据集；不允许通用调用隐式修改输入。')
    def input_paths(value):
        if isinstance(value, str):
            return [value] if path_exists(value) else []
        if isinstance(value, (list, tuple)):
            return [p for v in value for p in input_paths(v)]
        return []
    inputs = [p for k, v in lookup.items() if k not in out_keys for p in input_paths(v)]
    outputs = []
    for key in out_keys:
        value = lookup[key]
        if not isinstance(value, str) or not os.path.isabs(value):
            raise WorkerError('通用工具输出必须是可解析的绝对路径：' + str(value))
        if any(path_key(value) == path_key(source) for source in inputs):
            raise WorkerError('输出不能与输入相同：' + value)
        check_output(value)
        outputs.append((metadata[key].name, value))
    if len(outputs) != 1:
        raise WorkerError('当前安全发布只支持单个显式输出；多输出工具需要专门的事务适配器。')
    publications = []
    with ExitStack() as stack:
        for ws in sorted({safety.workspace(p) for _, p in outputs}, key=str.lower):
            stack.enter_context(safety.WorkspaceLease(ws + os.sep + '_lease_target'))
        for key, output in outputs:
            if lower == 'featurestojson_conversion':
                output = str(Path(output).with_suffix('.geojson' if
                    str(lookup.get('geojson', '')).upper() == 'GEOJSON' else '.json'))
            publication = safety.Publication(output, overwrite, safety.CURRENT_ROOT)
            ensure_output_container(publication.work)
            params[key] = publication.work
            publications.append(publication)
        with ExitStack() as tables:
            for (key, _), p in zip(outputs, publications):
                if lower in ('frequency_analysis', 'statistics_analysis', 'copyrows_management'):
                    params[key] = tables.enter_context(table_output(p.work))
            with arcpy.EnvManager(overwriteOutput=False):
                getattr(arcpy, name)(**params)
        # Validate every result before publishing any result.
        if any(not path_exists(p.work) for p in publications):
            raise WorkerError('通用工具未生成全部声明输出；暂存成果保留供诊断。')
        final_outputs = []
        for p in publications:
            path = p.publish()
            entry = {'path': path, 'exists': True}
            with contextlib.suppress(Exception):
                entry.update(describe_dataset(path))
            final_outputs.append(entry)
    return {'tool': name, 'parameters': logical_parameters, 'inputs': inputs,
            'outputs': final_outputs, 'retained_inputs': True,
            'target_replaced': any(p.backup for p in publications),
            'archived_outputs': [a for p in publications for a in p.archives]}


#: 通用层的操作表（由 worker 直接注册，不走 GEOPROCESSING / EDITING 那套流程）
META_OPERATIONS = {
    "list_arcpy_tools": op_list_arcpy_tools,
    "describe_arcpy_tool": op_describe_arcpy_tool,
    "run_arcpy_tool": op_run_arcpy_tool,
}
