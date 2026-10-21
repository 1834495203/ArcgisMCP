"""Draft/final separation, role layout and runnable export dependency contracts."""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from arcgis_mcp.core.history import History
from arcgis_mcp.core.workflow_plan import WorkflowPlan, export_script, graph_script
from arcgis_mcp.core.config import Settings
from arcgis_mcp.core.project import ProjectRegistry
from arcgis_mcp.core.tool import Services
from arcgis_mcp.tools.workflow import ExportWorkflow


class WorkflowPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.safety-plan-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = WorkflowPlan(self.root)
        self.layout = self.plan.layout()
        self.history = History(self.root)

    def unit(self, step='select', mode='final', source=None, code=None, outputs=None, deliverables=None):
        spec = {'workflow_id': 'soil', 'step_id': step, 'mode': mode,
            'description': '步骤说明 ' + step, 'reason': '需要验证并处理 ' + step,
            'code': code or 'arcpy.CopyFeatures(inputs["features"], outputs["result"])\n',
            'inputs': {'features': source or str(self.root / 'original.gdb/A')},
            'input_descriptions': {'features': '输入要素'},
            'outputs': {'result': '输出要素说明'} if outputs is None else outputs,
            'parameters': {'threshold': 7}, 'edit_inputs': {}, 'deliverables': deliverables or []}
        job = self.history.create('run_workflow', {}, 'test')
        self.plan.register(job, spec)
        return job, spec

    def complete(self, job, path):
        self.history.update(job, 'succeeded', result={'outputs': [{'name': 'result', 'path': path, 'exists': True}]})

    def test_roles_and_immutable_layout(self):
        for role in ('temporary', 'process', 'delivery'):
            self.assertEqual(self.plan.role(str(Path(self.layout[role]) / 'A')), role)
        self.assertEqual(self.plan.role(str(self.root / 'original.gdb/A')), 'original')
        self.assertEqual(self.plan.role(str(self.root / '.arcgis-mcp/scratch/scratch.gdb/A')), 'temporary')
        self.unit()
        with self.assertRaises(ValueError):
            self.plan.configure(process_gdb='another.gdb')
        with self.assertRaises(ValueError):
            self.plan.configure(process_gdb='process.gdb', temporary_gdb='process.gdb')

    def test_layout_can_be_corrected_before_execution_without_creating_gdb(self):
        changed = self.plan.configure(process_gdb='work.gdb')
        self.assertEqual(Path(changed['process']).name, 'work.gdb')
        self.assertFalse(Path(changed['process']).exists())

    def test_final_rejects_temporary_inputs_and_draft_deliverables(self):
        with self.assertRaisesRegex(ValueError, '临时数据'):
            self.unit(source=str(Path(self.layout['temporary']) / 'trial'))
        with self.assertRaisesRegex(ValueError, '交付'):
            self.unit(mode='draft', deliverables=['result'])
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.unit(source=str(Path(self.layout['process']) / 'unconfirmed'))

    def test_same_formal_step_cannot_be_silently_overwritten(self):
        job, _ = self.unit()
        with self.assertRaisesRegex(ValueError, '不要覆盖'):
            self.unit()
        self.history.update(job, 'failed', error='test')
        retry, _ = self.unit()
        self.assertNotEqual(job, retry)

    def test_successful_child_of_unconfirmed_parent_is_not_a_formal_input(self):
        parent, _ = self.unit()
        output = str(Path(self.layout['process']) / 'partial')
        child = self.history.create('workflow_step', {}, 'test')
        self.history.update(child, 'succeeded', result={'mode': 'final', 'parent_job_id': parent}, outputs=[output])
        self.history.update(parent, 'unknown', error='parent audit interruption')
        self.assertFalse(self.plan.confirmed_output(output))

    def test_draft_analysis_can_have_no_outputs_but_final_cannot(self):
        self.unit(mode='draft', outputs={})
        with self.assertRaises(ValueError):
            self.unit(step='analysis', outputs={})

    def test_listing_defaults_to_formal_and_keeps_code_on_request(self):
        self.unit(mode='draft')
        self.unit(step='approved')
        listed = self.plan.units('soil')
        self.assertEqual(len(listed), 1)
        self.assertNotIn('code', listed[0])
        self.assertEqual(len(self.plan.units('soil', include_drafts=True, detail=True)), 2)

    def test_export_is_readable_and_reconnects_to_rerun_outputs(self):
        draft, _ = self.unit(step='experiment', mode='draft', code='raise RuntimeError("EXPERIMENT")')
        self.complete(draft, str(Path(self.layout['temporary']) / 'trial'))
        first, _ = self.unit(step='select')
        first_path = str(Path(self.layout['process']) / 'old_select')
        self.complete(first, first_path)
        last, _ = self.unit(step='delivery', source=first_path, deliverables=['result'])
        self.complete(last, str(Path(self.layout['delivery']) / 'old_delivery'))
        code, inputs, params = export_script(self.plan.units('soil', include_drafts=True, detail=True))
        self.assertNotIn('EXPERIMENT', code)
        self.assertNotIn(first_path, code)
        self.assertIn('步骤说明 select', code)
        self.assertIn('arcpy.CopyFeatures(inputs["features"], outputs["result"])', code)
        self.assertEqual(len(inputs), 1)
        self.assertEqual(set(params), {'select', 'delivery'})
        calls = []
        class FakeStep:
            def __init__(self, index):
                self.index, self.outputs = index, []
            def __enter__(self):
                return self
            def output(self, role, label):
                calls.append(('role', role))
                return 'staging_' + str(self.index)
            def __exit__(self, *args):
                self.outputs = ['rebuilt_' + str(self.index)]
        class FakeWorkflow:
            inputs = {'input_1': 'new_original'}
            parameters = params
            def step(self, name, paths, parameters, reason):
                calls.append(('inputs', name, paths))
                return FakeStep(len([c for c in calls if c[0] == 'inputs']))
            def set_outputs(self, outputs):
                self.outputs = outputs
        workflow = FakeWorkflow()
        fake = types.SimpleNamespace(CopyFeatures=lambda src, out: calls.append(('copy', src, out)))
        with patch.dict(sys.modules, {'arcpy': fake}):
            exec(compile(code, 'export.py', 'exec'), {'workflow': workflow})
        self.assertIn(('copy', 'new_original', 'staging_1'), calls)
        self.assertIn(('copy', 'rebuilt_1', 'staging_2'), calls)
        self.assertIn(('role', 'delivery'), calls)
        self.assertEqual(workflow.outputs, {'delivery_result': 'rebuilt_2'})

    def test_export_does_not_publish_runnable_file_before_companions_complete(self):
        job, _ = self.unit()
        self.complete(job, str(Path(self.layout['process']) / 'selected'))
        settings = Settings(config_path=self.root / 'config.json', forced_project=self.root)
        tool = ExportWorkflow(Services(settings, ProjectRegistry(settings), types.SimpleNamespace()))
        ctx = types.SimpleNamespace(headers={})
        with patch('arcgis_mcp.tools.workflow.json.dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                tool.export_workflow(ctx, 'soil')
        self.assertEqual(list((self.root / 'workflows').glob('*.py')), [])
        exported = json.loads(tool.export_workflow(ctx, 'soil'))
        self.assertEqual(Path(exported['files']['script']).name, 'soil_2.py')
        for key in ('inputs', 'parameters', 'steps'):
            self.assertTrue(json.loads(Path(exported['files'][key]).read_text(encoding='utf-8')))

    def definition(self, step='first', inputs=None):
        return dict(workflow_id='graph', step_id=step, mode='final',
            description='节点说明', reason='节点理由', code='pass',
            inputs=inputs or {'features': str(self.root / 'original.gdb/A')},
            input_descriptions={'features': '输入'}, outputs={'result': '输出'},
            parameters={}, edit_inputs={}, deliverables=[])

    def test_saved_definitions_update_without_changing_history(self):
        spec = self.definition()
        self.plan.save_node(spec)
        self.plan.save_node({**spec, 'code': 'print("new body")'})
        self.assertEqual(self.plan.nodes('graph')[0]['code'], 'print("new body")')
        self.assertEqual(self.plan.workflows()[0]['nodes'], 1)
        self.assertEqual(self.plan.units('graph'), [])

    def test_graph_topology_rejects_missing_output_and_cycle(self):
        a = self.definition()
        b = self.definition('second', {'features': '@first.result'})
        code, sources, params, order = graph_script([b, a])
        self.assertEqual(order, ['first', 'second'])
        self.assertEqual(set(sources), {'first__features'})
        self.assertIn("_unit_0_outputs['result']", code)
        self.assertEqual(set(params), {'first', 'second'})
        with self.assertRaisesRegex(ValueError, '不存在'):
            graph_script([a, {**b, 'inputs': {'features': '@first.missing'}}])
        with self.assertRaisesRegex(ValueError, '循环'):
            graph_script([{**a, 'inputs': {'features': '@second.result'}}, b])

    def test_reference_resolution_requires_successful_mode_compatible_output(self):
        job, _ = self.unit(step='select')
        with self.assertRaises(ValueError):
            self.plan.resolve_references('soil', {'a': '@select.result'})
        self.complete(job, str(Path(self.layout['process']) / 'selected'))
        self.assertEqual(self.plan.resolve_references('soil', {'a': '@select.result'})['a'],
                         str(Path(self.layout['process']) / 'selected'))
        draft, _ = self.unit(step='trial', mode='draft')
        self.complete(draft, str(Path(self.layout['temporary']) / 'selected'))
        with self.assertRaises(ValueError):
            self.plan.resolve_references('soil', {'a': '@trial.result'}, 'final')
        self.assertTrue(self.plan.resolve_references('soil', {'a': '@trial.result'}, 'draft'))

    def test_export_definition_does_not_require_prior_run(self):
        self.plan.save_node(self.definition())
        settings = Settings(config_path=self.root / 'config.json', forced_project=self.root)
        tool = ExportWorkflow(Services(settings, ProjectRegistry(settings), types.SimpleNamespace()))
        result = json.loads(tool.export_workflow(types.SimpleNamespace(headers={}), 'graph', source='definition'))
        self.assertEqual(result['source'], 'definition')
        self.assertIn('节点说明', Path(result['files']['script']).read_text(encoding='utf-8'))

    def test_export_does_not_mix_graph_runs_or_export_unknown_parent(self):
        spec = self.definition()
        parents = []
        for value in (7, 9):
            parent = self.history.create('run_workflow', {'graph': {'workflow_id': 'graph'}}, 'test')
            self.history.update(parent, 'succeeded')
            job = self.history.create('workflow_step', {}, 'test')
            snapshot = {**spec, 'parent_job_id': parent, 'parameters': {'value': value}}
            from arcgis_mcp.core.history import encode
            import time
            with self.history.connect() as db:
                db.execute('INSERT INTO workflow_units VALUES(?,?,?,?,?,?)',
                    (job, 'graph', 'first', 'final', encode(snapshot), time.time()))
            self.complete(job, str(Path(self.layout['process']) / ('out_' + str(value))))
            parents.append(parent)
        settings = Settings(config_path=self.root / 'config.json', forced_project=self.root)
        tool = ExportWorkflow(Services(settings, ProjectRegistry(settings), types.SimpleNamespace()))
        ctx = types.SimpleNamespace(headers={})
        result = json.loads(tool.export_workflow(ctx, 'graph'))
        self.assertEqual(json.loads(Path(result['files']['parameters']).read_text())['first']['value'], 9)
        self.assertEqual(len(json.loads(Path(result['files']['steps']).read_text(encoding='utf-8'))), 1)
        self.history.update(parents[-1], 'unknown', error='publication interrupted')
        with self.assertRaisesRegex(Exception, '未知'):
            tool.export_workflow(ctx, 'graph')
        # A newer submission without any child must not silently export an older run.
        latest = self.history.create('run_workflow', {'graph': {'workflow_id': 'graph'}}, 'test')
        self.history.update(latest, 'unknown', error='child never returned')
        self.assertEqual(self.plan.latest_graph_run('graph')['job_id'], latest)
        with self.assertRaisesRegex(Exception, '未知'):
            tool.export_workflow(ctx, 'graph')


if __name__ == '__main__':
    unittest.main()
