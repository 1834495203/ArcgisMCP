"""Workflow SDK contracts; fake ArcPy operates only on private plain fixture files."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'src/arcgis_mcp/core'))
from history import History
import workflow_runtime as runtime
from ops import safety
from ops.common import WorkerError
from ops import workflow as supervisor


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='.safety-workflow-', dir=ROOT)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.gdb = self.root / 'work.gdb'
        self.gdb.mkdir()
        self.source = self.root / 'source'
        self.source.write_text('[1, 2]')
        self.history = History(self.root)
        self.parent = self.history.create('run_workflow', {}, 'test')
        self.request = dict(root=str(self.root), job_id=self.parent, output_gdb=str(self.gdb),
            reason='测试脚本工作流', script_sha256=hashlib.sha256(b'script').hexdigest(),
            inputs={'source': str(self.source)}, parameters={'value': 7})
        fake = types.SimpleNamespace(ResetEnvironments=lambda: None,
            GetInstallInfo=lambda: {'Version': 'fake'}, env=types.SimpleNamespace(),
            GetMessages=lambda: 'tool completed', ListEnvironments=lambda: ['extent'],
            ValidateTableName=lambda value, ws: value)
        self.enterContext(patch.dict(sys.modules, {'arcpy': fake}))
        self.enterContext(patch.object(runtime, 'path_exists', lambda p: Path(p).exists()))
        self.enterContext(patch.object(safety, 'path_exists', lambda p: Path(p).exists()))
        self.enterContext(patch.object(safety, 'copy_for_edit', shutil.copyfile))
        self.enterContext(patch.object(safety, 'move_dataset', os.replace))
        self.enterContext(patch.object(runtime, 'ensure_output_container', lambda p: None))
        for key in ('CURRENT_ROOT', 'CURRENT_JOB', 'AUTO_CREATE_GDB', 'OUTPUT_WORKSPACE', 'DEFER_LEASE_RELEASE'):
            self.enterContext(patch.object(safety, key, getattr(safety, key)))
        self.addCleanup(safety.release_leases)
        self.wf = runtime.Workflow(self.request)
        self.wf.prepare()

    def test_step_copy_publish_and_precise_lineage(self):
        snapshot = self.wf.inputs['source']
        with self.wf.step('calculate', [snapshot], {'value': 7}) as step:
            work = step.edit_copy(snapshot)
            Path(work).write_text('[7, 2]')
        self.wf.set_outputs({'final': step.result})
        result = self.wf.finish()
        self.assertEqual(self.source.read_text(), '[1, 2]')
        self.assertEqual(Path(snapshot).read_text(), '[1, 2]')
        self.assertEqual(Path(step.result).read_text(), '[7, 2]')
        lineage = self.history.lineage(step.result, detail=False)
        self.assertEqual(len(lineage['steps']), 2)
        self.assertTrue(all(s['parent_job_id'] == self.parent for s in lineage['steps']))
        self.assertEqual(result['outputs'][0]['path'], step.result)

    def test_read_only_inputs_are_references_not_false_snapshots(self):
        wf = runtime.Workflow({**self.request, 'input_mode': 'read_only'})
        with patch.object(safety, 'copy_for_edit', side_effect=AssertionError('must not copy')):
            wf.prepare()
        self.assertEqual(wf.inputs['source'], str(self.source))
        with wf.step('derive', [wf.inputs['source']]) as step:
            Path(step.output()).write_text('[42]')
        wf.set_outputs({'result': step.result})
        result = wf.finish()
        self.assertIsNone(result['input_bindings']['source']['snapshot'])
        self.assertEqual(result['input_mode'], 'read_only')
        self.assertNotIn('source_modified', self.history.get(step.job_id)['result'])
        self.assertEqual(self.source.read_text(), '[1, 2]')
        events = self.history.events(self.parent, limit=100)['events']
        ref = next(e for e in events if e['stage'] == 'input_reference')
        self.assertFalse(ref['details']['enforced_by_runtime'])

    def test_graph_nodes_have_independent_snapshot_and_named_outputs(self):
        spec = dict(workflow_id='graph', step_id='derive', mode='final', code='pass',
            reason='需要生成结果', description='节点说明', inputs={'source': str(self.source)},
            outputs={'result': '结果'}, input_descriptions={'source': '原始数据'}, parameters={})
        wf = runtime.Workflow({**self.request, 'graph': {'workflow_id': 'graph', 'nodes': [spec]}})
        wf.prepare()
        with wf.step('derive', [wf.inputs['source']]) as step:
            Path(step.output()).write_text('[42]')
        from workflow_plan import WorkflowPlan
        units = WorkflowPlan(self.root).units('graph', detail=True)
        self.assertEqual(units[0]['actual_outputs'][0]['name'], 'result')
        self.assertEqual(units[0]['inputs']['source'], str(self.source))
        self.assertEqual(units[0]['parent_job_id'], self.parent)

    def test_graph_snapshot_audit_failure_stops_before_body(self):
        event = self.wf.event
        def fail(stage, details):
            if stage == 'step_started':
                raise OSError('node audit interrupted')
            return event(stage, details)
        with patch.object(self.wf, 'event', fail):
            with self.assertRaisesRegex(OSError, 'audit interrupted'):
                with self.wf.step('node', [self.wf.inputs['source']]):
                    self.fail('node body must not run')
        self.assertEqual(self.history.get(self.wf.steps[-1])['state'], 'failed')
        self.assertEqual(self.source.read_text(), '[1, 2]')

    def test_failed_step_preserves_success_and_prevents_parent_success(self):
        source = self.wf.inputs['source']
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with self.wf.step('fail', [source]) as step:
                Path(step.edit_copy(source)).write_text('[99]')
                raise RuntimeError('synthetic')
        self.wf.set_outputs({'before': source})
        with self.assertRaises(WorkerError):
            self.wf.finish()
        self.assertEqual(self.history.get(step.job_id)['state'], 'failed')
        self.assertEqual(Path(source).read_text(), '[1, 2]')
        self.assertEqual(self.history.get(self.wf.steps[0])['state'], 'succeeded')

    def test_raw_original_and_internal_temp_cannot_be_step_inputs(self):
        with self.assertRaises(WorkerError):
            with self.wf.step('bad', [str(self.source)]):
                pass

    def test_no_output_and_missing_output_fail(self):
        for create in (False, True):
            wf = runtime.Workflow({**self.request, 'inputs': {}})
            wf.known.update(self.wf.known)
            with self.assertRaises(WorkerError):
                with wf.step('missing', [self.wf.inputs['source']]) as step:
                    if create:
                        step.output()

    def test_publication_audit_interruption_is_unknown_and_data_is_retained(self):
        event = self.wf.event
        def interrupt(stage, details):
            if stage == 'published':
                raise OSError('audit failure after publish')
            event(stage, details)
        with patch.object(self.wf, 'event', interrupt):
            with self.assertRaises(OSError):
                with self.wf.step('interrupt', [self.wf.inputs['source']]) as step:
                    Path(step.edit_copy(self.wf.inputs['source'])).write_text('[8]')
        self.assertEqual(self.history.get(step.job_id)['state'], 'unknown')
        self.assertEqual(Path(step.publications[0].final).read_text(), '[8]')
        self.assertEqual(self.source.read_text(), '[1, 2]')
        self.assertEqual(self.history.lineage(step.publications[0].final)['total_steps'], 0)

    def test_stdout_and_stderr_are_persistent(self):
        import io
        for name in ('stdout', 'stderr'):
            runtime.AuditStream(self.wf, name, io.StringIO()).write('hello')
        stages = [e['stage'] for e in History(self.root).events(self.parent, limit=100)['events']]
        self.assertIn('stdout', stages)
        self.assertIn('stderr', stages)

    def test_generated_dataset_can_have_no_input(self):
        with self.wf.step('generate', []) as step:
            Path(step.output()).write_text('[42]')
        self.wf.set_outputs({'generated': step.result})
        self.assertEqual(self.wf.finish()['outputs'][0]['path'], step.result)

    def test_audit_failure_stops_before_copy(self):
        with patch.object(self.wf, 'event', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                with self.wf.step('copy', [self.wf.inputs['source']]):
                    self.fail('must not execute body')
        self.assertEqual(self.source.read_text(), '[1, 2]')

    def test_caller_validation_is_persistent_and_required_failure_cannot_be_caught_away(self):
        source = self.wf.inputs['source']
        with self.assertRaises(WorkerError):
            with self.wf.step('validate', [source], reason='验证由脚本定义') as step:
                step.edit_copy(source)
                try:
                    step.check('caller threshold', False, {'value': 2, 'expected': 7})
                except WorkerError:
                    pass
        logs = self.history.events(step.job_id)['events']
        self.assertEqual(logs[0]['stage'], 'validation')
        self.assertFalse(logs[0]['details']['passed'])

    def test_optional_validation_records_failure_without_business_override(self):
        source = self.wf.inputs['source']
        with self.wf.step('optional', [source]) as step:
            step.edit_copy(source)
            step.check('diagnostic', False, {'count': 0}, required=False)
        self.assertEqual(self.history.get(step.job_id)['state'], 'succeeded')
        self.assertFalse(self.history.get(step.job_id)['result']['checks'][0]['passed'])

    def test_multiple_edit_copies_reserve_distinct_names_before_publication(self):
        source = self.wf.inputs['source']
        with self.wf.step('variants', [source]) as step:
            first = step.edit_copy(source)
            second = step.edit_copy(source)
            Path(first).write_text('[7]')
            Path(second).write_text('[8]')
        self.assertEqual(len(set(step.outputs)), 2)
        self.assertEqual([Path(p).read_text() for p in step.outputs], ['[7]', '[8]'])
        self.assertEqual(Path(source).read_text(), '[1, 2]')

    def test_supervisor_timeout_is_unknown_not_success(self):
        import subprocess
        with patch.object(safety, 'CURRENT_JOB', self.parent), patch.object(
                supervisor.subprocess, 'run', side_effect=subprocess.TimeoutExpired('python', 10)):
            with self.assertRaises(TimeoutError):
                supervisor.run_workflow('pass', 'test.py', 'timeout test', {}, {}, str(self.gdb), 10, True)
        self.assertIn('workflow_timeout', [e['stage'] for e in self.history.events(self.parent, limit=100)['events']])

    def test_supervisor_lost_child_outcome_is_unknown(self):
        with patch.object(safety, 'CURRENT_JOB', self.parent), patch.object(
                supervisor.subprocess, 'run', return_value=types.SimpleNamespace(returncode=9)):
            with self.assertRaisesRegex(RuntimeError, '状态未知'):
                supervisor.run_workflow('pass', 'test.py', 'lost child test', {}, {}, str(self.gdb), 10, True)

    def test_watchdog_exits_when_supervisor_disappears(self):
        with patch.object(runtime, 'process_alive', return_value=False), patch.object(
                runtime.os, '_exit', side_effect=SystemExit(124)) as exit_process:
            with self.assertRaises(SystemExit):
                runtime.watchdog(123, float('inf'))
            exit_process.assert_called_once_with(124)

    def test_failed_child_with_unknown_publication_keeps_parent_unknown(self):
        child = self.history.create('workflow_step', {}, 'test')
        self.history.update(child, 'unknown', error='publication audit interruption')
        def finish(args, **kwargs):
            folder = Path(args[-1]).parent
            (folder / 'outcome.json').write_text(json.dumps({'error': 'audit failed', 'steps': [child]}))
            return types.SimpleNamespace(returncode=1)
        with patch.object(safety, 'CURRENT_JOB', self.parent), patch.object(
                supervisor.subprocess, 'run', side_effect=finish):
            with self.assertRaisesRegex(RuntimeError, '禁止自动重放'):
                supervisor.run_workflow('pass', 'test.py', 'audit interruption', {}, {}, str(self.gdb), 10, True)


if __name__ == '__main__':
    unittest.main()
