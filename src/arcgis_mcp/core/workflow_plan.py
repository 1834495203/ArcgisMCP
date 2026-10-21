"""Persistent incremental workflow plans and controlled data roles (stdlib, worker-safe)."""
import hashlib
import json
from pathlib import Path
import re
import time

try:  # Service package and standalone ArcPy worker load the same database implementation.
    from .history import History, canonical, encode
except ImportError:
    from history import History, canonical, encode


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value):
        raise ValueError('流程/步骤/输出 ID 须以英文字母开头，仅含字母数字下划线，最长 64 字符。')
    return value


class WorkflowPlan:
    def __init__(self, root):
        self.history = History(root)
        self.root = self.history.root
        with self.history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workflow_layout (id INTEGER PRIMARY KEY, value TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS workflow_units (
                job_id TEXT PRIMARY KEY, workflow_id TEXT, step_id TEXT, mode TEXT,
                spec TEXT NOT NULL, created REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS workflow_units_flow ON workflow_units(workflow_id,created)')
            db.execute('''CREATE TABLE IF NOT EXISTS workflow_nodes (
                workflow_id TEXT NOT NULL, step_id TEXT NOT NULL, spec TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(workflow_id,step_id))''')

    def save_node(self, spec):
        """Definitions are editable; execution keeps its own immutable code snapshot."""
        identifier(spec['workflow_id'])
        identifier(spec['step_id'])
        validate_node(spec)
        compile(unit_block(spec, '{}', 0), '<node-body>', 'exec')
        with self.history.connect() as db:
            db.execute('INSERT OR REPLACE INTO workflow_nodes VALUES(?,?,?,?)',
                (spec['workflow_id'], spec['step_id'], encode(spec), time.time()))

    def nodes(self, workflow_id):
        identifier(workflow_id)
        with self.history.connect() as db:
            rows = db.execute('SELECT spec FROM workflow_nodes WHERE workflow_id=? ORDER BY step_id',
                              (workflow_id,)).fetchall()
        return [json.loads(row['spec']) for row in rows]

    def workflows(self):
        with self.history.connect() as db:
            rows = db.execute('SELECT workflow_id,COUNT(*) AS nodes,MAX(updated) AS updated '
                              'FROM workflow_nodes GROUP BY workflow_id ORDER BY updated DESC').fetchall()
        return [dict(row) for row in rows]

    def latest_graph_run(self, workflow_id):
        identifier(workflow_id)
        with self.history.connect() as db:
            rows = db.execute("SELECT id,args FROM operations WHERE op='run_workflow' ORDER BY created DESC,id DESC").fetchall()
        for row in rows:
            graph = json.loads(row['args']).get('graph') or {}
            if graph.get('workflow_id') == workflow_id:
                return self.history.get(row['id'])
        return None

    def resolve_references(self, workflow_id, inputs, mode='final'):
        resolved = {}
        units = self.units(workflow_id, include_drafts=True, detail=True)
        for alias, value in inputs.items():
            ref = parse_reference(value)
            if not ref:
                resolved[alias] = value
                continue
            step_id, output = ref
            matches = [u for u in units if u['step_id'] == step_id and u['state'] == 'succeeded'
                       and (not u.get('parent_job_id') or u.get('parent_state') == 'succeeded')
                       and (mode == 'draft' or u['mode'] == 'final')]
            if not matches:
                raise ValueError('引用没有成功且模式兼容的上游节点：' + value)
            paths = {o['name']: o['path'] for o in matches[-1]['actual_outputs']}
            if output not in paths:
                raise ValueError('上游未声明该输出：' + value)
            resolved[alias] = paths[output]
        return resolved

    def layout(self, process_gdb=None, create=True):
        with self.history.connect() as db:
            row = db.execute('SELECT value FROM workflow_layout WHERE id=1').fetchone()
        if row:
            return json.loads(row['value'])
        return self.configure(process_gdb=process_gdb) if create else None

    def configure(self, process_gdb=None, temporary_gdb=None, delivery_gdb=None, original_roots=None):
        defaults = {'process': process_gdb or 'process.gdb',
                    'temporary': temporary_gdb or 'temporary.gdb',
                    'delivery': delivery_gdb or 'delivery.gdb'}
        layout = {}
        for role, value in defaults.items():
            path = Path(value)
            path = (path if path.is_absolute() else self.root / path).resolve()
            try:
                path.relative_to(self.root)
            except ValueError:
                raise ValueError('工作流数据库必须在当前项目内。')
            if path.suffix.lower() != '.gdb' or '.arcgis-mcp' in path.parts:
                raise ValueError('角色目录必须是项目内的业务 GDB，不能使用 .arcgis-mcp 内部路径。')
            if any(parent.suffix.lower() == '.gdb' for parent in path.parents):
                raise ValueError('角色 GDB 不能嵌套在另一个 GDB 内。')
            layout[role] = str(path)
        if len({canonical(p) for p in layout.values()}) != 3:
            raise ValueError('过程、临时和交付数据库必须互不相同。')
        roots = [str((Path(p) if Path(p).is_absolute() else self.root / p).resolve())
                 for p in (original_roots or [])]
        if any(canonical(p) == canonical(g) for p in roots for g in layout.values()):
            raise ValueError('原始数据根不能与输出角色数据库相同。')
        layout['original_roots'] = roots
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT value FROM workflow_layout WHERE id=1').fetchone()
            if existing:
                old = json.loads(existing['value'])
                if old != layout:
                    used = db.execute('SELECT 1 FROM workflow_units LIMIT 1').fetchone()
                    started = db.execute("SELECT 1 FROM operations WHERE op='workflow_step' OR "
                        "(op='run_workflow' AND state!='failed') LIMIT 1").fetchone()
                    if used or started:
                        raise ValueError('项目数据布局在开始执行后已固定；不得按步骤更换 GDB。')
                    db.execute('UPDATE workflow_layout SET value=? WHERE id=1', (encode(layout),))
                    return layout
                return old
            db.execute('INSERT INTO workflow_layout VALUES(1,?)', (encode(layout),))
        return layout

    def role(self, path, layout=None):
        path = Path(path).resolve()
        try:
            path.relative_to(self.root / '.arcgis-mcp')
            return 'temporary'
        except ValueError:
            pass
        layout = layout or self.layout()
        for role in ('temporary', 'delivery', 'process'):
            try:
                path.relative_to(Path(layout[role]))
                return role
            except ValueError:
                pass
            files = self.root / (role + '_files')
            try:
                path.relative_to(files)
                return role
            except ValueError:
                pass
        return 'original'

    def register(self, job_id, spec):
        identifier(spec['workflow_id'])
        identifier(spec['step_id'])
        if spec['mode'] not in ('draft', 'final') or not spec['reason'].strip() or not spec.get('description', '').strip():
            raise ValueError('步骤必须声明 draft/final、非空理由与说明。')
        if set(spec.get('input_descriptions', {})) != set(spec['inputs']) or any(
                not str(v).strip() for v in spec.get('input_descriptions', {}).values()) or any(
                not str(v).strip() for v in spec['outputs'].values()):
            raise ValueError('输入说明必须完整对应每个输入，输入/输出说明不能为空。')
        for alias in list(spec['inputs']) + list(spec['outputs']):
            identifier(alias)
        if spec['mode'] == 'draft' and spec.get('deliverables'):
            raise ValueError('实验步骤不能写入交付区。')
        if spec['mode'] == 'final' and not spec['outputs']:
            raise ValueError('正式步骤必须声明输出；只读分析请使用 draft。')
        if set(spec.get('edit_inputs', {})) - set(spec['outputs']) or any(
                key not in spec['inputs'] for key in spec.get('edit_inputs', {}).values()):
            raise ValueError('edit_inputs 必须将输出别名映射到已声明的输入别名。')
        if set(spec.get('deliverables', [])) - set(spec['outputs']):
            raise ValueError('deliverables 必须是已声明的输出别名。')
        if spec['mode'] == 'final' and any(self.role(p) == 'temporary' for p in spec['inputs'].values()):
            raise ValueError('正式步骤不能依赖临时数据；请从原始数据或正式过程成果重算。')
        if spec['mode'] == 'final' and any(self.role(p) in ('process', 'delivery') and not self.confirmed_output(p)
                                         for p in spec['inputs'].values()):
            raise ValueError('正式过程/交付输入必须来自已成功审计的正式成果，不能使用失败或结果未确认的数据。')
        spec = {**spec, 'script_sha256': hashlib.sha256(spec['code'].encode('utf-8')).hexdigest()}
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if spec['mode'] == 'final':
                prior = db.execute("SELECT u.job_id,o.state FROM workflow_units u JOIN operations o ON o.id=u.job_id "
                    "WHERE u.workflow_id=? AND u.step_id=? AND u.mode='final' AND o.state!='failed'",
                    (spec['workflow_id'], spec['step_id'])).fetchone()
                if prior:
                    raise ValueError('该正式步骤已提交或结果未确认；不要覆盖/重放，请查询任务或使用新的步骤 ID：' + prior['job_id'])
            db.execute('INSERT INTO workflow_units VALUES(?,?,?,?,?,?)',
                (job_id, spec['workflow_id'], spec['step_id'], spec['mode'], encode(spec), time.time()))
        self.history.event(job_id, 'workflow_unit_registered', {
            'workflow_id': spec['workflow_id'], 'step_id': spec['step_id'], 'mode': spec['mode'],
            'reason': spec['reason'], 'description': spec.get('description', ''),
            'inputs': spec['inputs'], 'outputs': spec['outputs'], 'script_sha256': spec['script_sha256']})

    def confirmed_output(self, path):
        with self.history.connect() as db:
            rows = db.execute("SELECT o.result FROM edges e JOIN operations o ON o.id=e.operation_id "
                "WHERE e.output=? AND o.state='succeeded'", (canonical(path),)).fetchall()
            for row in rows:
                payload = json.loads(row['result'] or '{}')
                parent = payload.get('parent_job_id')
                parent_state = db.execute('SELECT state FROM operations WHERE id=?', (parent,)).fetchone() if parent else None
                if payload.get('mode', 'final') == 'final' and (not parent or (parent_state and parent_state['state'] == 'succeeded')):
                    return True
            rows = db.execute("SELECT o.result FROM workflow_units u JOIN operations o ON o.id=u.job_id "
                "WHERE u.mode='final' AND o.state='succeeded'").fetchall()
        return any(canonical(out['path']) == canonical(path) for row in rows
                   for out in json.loads(row['result'] or '{}').get('outputs', []))

    def units(self, workflow_id, include_drafts=False, detail=False):
        identifier(workflow_id)
        with self.history.connect() as db:
            rows = db.execute('SELECT job_id,spec,created FROM workflow_units WHERE workflow_id=? '
                              'ORDER BY created,job_id', (workflow_id,)).fetchall()
        result = []
        for row in rows:
            spec = json.loads(row['spec'])
            if spec['mode'] == 'draft' and not include_drafts:
                continue
            job = self.history.get(row['job_id'])
            outputs = (job.get('result') or {}).get('outputs', [])
            item = {**spec, 'job_id': row['job_id'], 'state': job['state'], 'created': row['created'],
                'actual_outputs': outputs, 'input_roles': {k: self.role(p) for k, p in spec['inputs'].items()}}
            if spec.get('parent_job_id'):
                item['parent_state'] = self.history.get(spec['parent_job_id'])['state']
            item['output_roles'] = {alias: 'temporary' if spec['mode'] == 'draft' else (
                'delivery' if alias in spec.get('deliverables', []) else 'process') for alias in spec['outputs']}
            with self.history.connect() as db:
                checks = db.execute("SELECT details FROM job_events WHERE job_id=? AND stage='validation' "
                                    'ORDER BY sequence' + ('' if detail else ' LIMIT 21'), (row['job_id'],)).fetchall()
            item['validations'] = [json.loads(check['details']) for check in (checks if detail else checks[:20])]
            item['validations_truncated'] = not detail and len(checks) > 20
            if not detail:
                item.pop('code', None)
            result.append(item)
        return result


def unit_block(spec, input_expression, index):
    """Generate one isolated namespace per body, preserving edits, parameters and explanation."""
    import textwrap
    var = '_unit_' + str(index)
    lines = ['# ' + spec['step_id'] + ': ' + ' '.join(spec['description'].splitlines()),
             '# Reason: ' + ' '.join(spec['reason'].splitlines()),
             'def _body_' + str(index) + '(inputs, outputs, parameters, step):',
             '    import arcpy', textwrap.indent(spec['code'].rstrip() or 'pass', '    '), '',
             '_inputs = ' + input_expression,
             '_parameters = workflow.parameters.get(' + repr(spec['step_id']) + ', ' + repr(spec['parameters']) + ')',
             'with workflow.step(' + repr(spec['step_id']) + ', list(_inputs.values()), _parameters, reason=' + repr(spec['reason']) +
             (', allow_no_output=True' if not spec['outputs'] else '') + ') as ' + var + ':',
             '    _outputs = {}']
    annotations = ['# Input ' + alias + ': ' + ' '.join(description.splitlines())
                   for alias, description in spec.get('input_descriptions', {}).items()]
    annotations += ['# Output ' + alias + ': ' + ' '.join(description.splitlines())
                    for alias, description in spec['outputs'].items()]
    lines = annotations + lines
    for alias in spec['outputs']:
        role = 'temporary' if spec['mode'] == 'draft' else ('delivery' if alias in spec.get('deliverables', []) else 'process')
        source = spec.get('edit_inputs', {}).get(alias)
        expression = (var + '.edit_copy(_inputs[' + repr(source) + '], role=' + repr(role) + ', label=' + repr(spec['step_id'] + '_' + alias) + ')'
                      if source else var + '.output(role=' + repr(role) + ', label=' + repr(spec['step_id'] + '_' + alias) + ')')
        lines.append('    _outputs[' + repr(alias) + '] = ' + expression)
    lines.extend(['    _body_' + str(index) + '(_inputs, _outputs, _parameters, ' + var + ')',
                  var + '_outputs = dict(zip(' + repr(list(spec['outputs'])) + ', ' + var + '.outputs))'])
    return '\n'.join(lines) + '\n'


def unit_script(spec):
    return unit_block(spec, 'dict(workflow.inputs)', 0) + ('\nworkflow.set_outputs(_unit_0_outputs)\n'
        if spec['outputs'] else '\nworkflow.finish_analysis()\n')


def parse_reference(value):
    if not isinstance(value, str):
        raise ValueError('输入必须是路径或 @节点.输出 引用。')
    if not value.startswith('@'):
        return None
    parts = value[1:].split('.')
    if len(parts) != 2:
        raise ValueError('节点引用格式为 @step_id.output_alias。')
    return tuple(identifier(p) for p in parts)


def validate_node(spec):
    if not spec.get('reason', '').strip() or not spec.get('description', '').strip():
        raise ValueError('节点必须有说明与理由。')
    if not spec.get('outputs'):
        raise ValueError('保存的流程节点必须声明输出；纯分析使用 draft 单步入口。')
    if set(spec.get('input_descriptions', {})) != set(spec['inputs']):
        raise ValueError('输入说明必须逐项对应。')
    for alias in list(spec['inputs']) + list(spec['outputs']):
        identifier(alias)
    if any(not str(v).strip() for v in list(spec['outputs'].values()) + list(spec['input_descriptions'].values())):
        raise ValueError('输入输出说明不能为空。')
    for value in spec['inputs'].values():
        parse_reference(value)
    if set(spec.get('edit_inputs', {})) - set(spec['outputs']) or any(
            alias not in spec['inputs'] for alias in spec.get('edit_inputs', {}).values()):
        raise ValueError('edit_inputs 必须为已声明的输出到输入映射。')
    if set(spec.get('deliverables', [])) - set(spec['outputs']):
        raise ValueError('交付必须是已声明的输出。')


def graph_script(nodes):
    """Compile a saved DAG; all edges bind to the outputs of this execution."""
    if not nodes:
        raise ValueError('流程没有节点。')
    by_id = {identifier(n['step_id']): n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError('流程节点 ID 重复。')
    dependencies = {}
    for name, spec in by_id.items():
        validate_node(spec)
        refs = [parse_reference(v) for v in spec['inputs'].values()]
        dependencies[name] = set()
        for ref in filter(None, refs):
            parent, output = ref
            if parent not in by_id or output not in by_id[parent]['outputs']:
                raise ValueError('不存在的节点输出引用：@' + parent + '.' + output)
            dependencies[name].add(parent)
    ordered, remaining = [], set(by_id)
    while remaining:
        ready = sorted(n for n in remaining if dependencies[n].issubset(set(ordered)))
        if not ready:
            raise ValueError('流程存在循环依赖。')
        ordered.extend(ready)
        remaining.difference_update(ready)
    sources, params, variables, blocks, final = {}, {}, {}, [], {}
    for index, name in enumerate(ordered):
        spec = {**by_id[name], 'mode': 'final'}
        bindings = {}
        for alias, value in spec['inputs'].items():
            ref = parse_reference(value)
            if ref:
                bindings[alias] = variables[ref]
            else:
                key = name + '__' + alias
                sources[key] = value
                bindings[alias] = 'workflow.inputs[' + repr(key) + ']'
        expression = '{' + ', '.join(repr(k) + ': ' + v for k, v in bindings.items()) + '}'
        blocks.append(unit_block(spec, expression, index))
        params[name] = spec.get('parameters', {})
        for alias in spec['outputs']:
            variable = '_unit_' + str(index) + '_outputs[' + repr(alias) + ']'
            variables[(name, alias)] = variable
            if alias in spec.get('deliverables', []):
                final[name + '_' + alias] = variable
    if not final:
        parents = set().union(*dependencies.values())
        final = {name + '_' + alias: variables[(name, alias)] for name in ordered if name not in parents
                 for alias in by_id[name]['outputs']}
    blocks.append('workflow.set_outputs({' + ', '.join(repr(k) + ': ' + v for k, v in final.items()) + '})\n')
    code = '\n'.join(blocks)
    compile(code, '<workflow-graph>', 'exec')
    return code, sources, params, ordered


def export_script(units):
    """Export committed final units only; dependencies bind to rerun outputs, never old paths."""
    selected = [u for u in units if u['mode'] == 'final' and u['state'] == 'succeeded']
    if not selected:
        raise ValueError('没有成功的正式步骤可导出。')
    paths, sources, params = {}, {}, {}
    blocks = ['# Generated confirmed workflow. Run via run_workflow or arcgis_mcp.workflow.\n']
    final = {}
    for index, spec in enumerate(selected):
        bindings = {}
        for alias, path in spec['inputs'].items():
            key = canonical(path)
            if key in paths:
                bindings[alias] = paths[key]
            else:
                source_alias = next((k for k, v in sources.items() if canonical(v) == key), None)
                if source_alias is None:
                    source_alias = 'input_' + str(len(sources) + 1)
                    sources[source_alias] = path
                bindings[alias] = 'workflow.inputs[' + repr(source_alias) + ']'
        expression = '{' + ', '.join(repr(k) + ': ' + v for k, v in bindings.items()) + '}'
        blocks.append(unit_block(spec, expression, index))
        params[spec['step_id']] = spec['parameters']
        for output in spec['actual_outputs']:
            alias = output['name']
            variable = '_unit_' + str(index) + '_outputs[' + repr(alias) + ']'
            paths[canonical(output['path'])] = variable
            if alias in spec.get('deliverables', []):
                final[spec['step_id'] + '_' + alias] = variable
        if index == len(selected) - 1 and not final:
            final = {spec['step_id'] + '_' + alias: '_unit_' + str(index) + '_outputs[' + repr(alias) + ']'
                     for alias in spec['outputs']}
    blocks.append('workflow.set_outputs({' + ', '.join(repr(k) + ': ' + v for k, v in final.items()) + '})\n')
    return '\n'.join(blocks), sources, params
