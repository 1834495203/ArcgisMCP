"""Declaration, durable audit and copy/publish contracts without ArcPy."""
import contextlib
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
from arcgis_mcp.core.history import History
from ops import safety, common, unadapted


class UnadaptedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.safety-unadapted-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.write_text('[1, 1, 2]')
        self.args = dict(tool='Deduplicate_management', reason='没有固定去重封装',
            parameters={'in_table': str(self.source), 'fields': ['VALUE']},
            input_parameters=['in_table'], edit_parameter='in_table')
        self.history = History(self.root)
        self.job = self.history.create('run_unadapted_tool', self.args, 'test')
        def param(name, datatype='GPString', direction='Input', derived=False):
            return types.SimpleNamespace(name=name, datatype=datatype, direction=direction,
                parameterType='Derived' if derived else 'Required', multiValue=False)
        self.metadata = [param('in_table', 'DETable'), param('fields'),
                         param('out_table', 'DETable', 'Output', True)]
        def execute(**params):
            path = Path(params['in_table'])
            path.write_text('[1, 2]')
            return types.SimpleNamespace(getMessages=lambda: 'processed private data')
        self.execute = execute
        self.fake = types.SimpleNamespace(ListTools=lambda *a: ['Deduplicate_management'],
            GetParameterInfo=lambda name: self.metadata, GetMessages=lambda: 'ArcPy diagnostic',
            EnvManager=lambda **kw: contextlib.nullcontext(),
            Deduplicate_management=lambda **kw: self.execute(**kw))
        for target, value in [('CURRENT_ROOT', str(self.root)), ('CURRENT_JOB', self.job),
                               ('DEFER_LEASE_RELEASE', False), ('OUTPUT_WORKSPACE', None)]:
            self.enterContext(patch.object(safety, target, value))
        self.enterContext(patch.dict(sys.modules, {'arcpy': self.fake}))
        self.enterContext(patch.object(unadapted, 'path_exists', lambda p: Path(p).exists()))
        self.enterContext(patch.object(safety, 'path_exists', lambda p: Path(p).exists()))
        self.enterContext(patch.object(safety, 'copy_for_edit', shutil.copyfile))
        self.enterContext(patch.object(safety, 'move_dataset', os.replace))

    def run_tool(self, **overrides):
        return unadapted.run_unadapted_tool(**{**self.args, **overrides})

    def test_copy_edit_and_lineage_reason(self):
        result = self.run_tool()
        self.assertEqual(json.loads(self.source.read_text()), [1, 1, 2])
        self.assertEqual(json.loads(Path(result['primary_output']).read_text()), [1, 2])
        self.history.update(self.job, 'succeeded', result=result, inputs=result['inputs'],
                            outputs=[p['path'] for p in result['outputs']])
        chain = History(self.root).lineage(result['primary_output'], detail=False)
        self.assertEqual(chain['steps'][0]['reason'], self.args['reason'])

    def test_blank_reason_and_missing_declaration_fail_before_call(self):
        for kwargs in ({'reason': ' '}, {'input_parameters': []},
                       {'edit_parameter': None}, {'output_parameter': 'out_table'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(common.WorkerError):
                self.run_tool(**kwargs)
        self.assertEqual(self.source.read_text(), '[1, 1, 2]')
        self.assertEqual(self.history.events(self.job, limit=100)['events'][-1]['stage'], 'failed')

    def test_undeclared_input_rejected(self):
        with self.assertRaisesRegex(common.WorkerError, '显式声明'):
            self.run_tool(parameters={'in_table': str(self.source), 'fields': 'VALUE'},
                          input_parameters=['fields'], edit_parameter='fields')

    def test_failure_keeps_source_and_durable_diagnostics(self):
        def fail(**params):
            Path(params['in_table']).write_text('partial')
            raise RuntimeError('synthetic failure')
        self.execute = fail
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            self.run_tool()
        self.assertEqual(self.source.read_text(), '[1, 1, 2]')
        events = History(self.root).events(self.job, limit=100)['events']
        self.assertIn('arcpy_error', [e['stage'] for e in events])
        self.assertNotIn('published', [e['stage'] for e in events])
        self.assertTrue(any(e['stage'] == 'executing' and e['details']['reason'] for e in events))

    def test_logs_paginate_after_restart(self):
        self.run_tool()
        first = History(self.root).events(self.job, limit=2)
        second = History(self.root).events(self.job, after=first['next_after'], limit=100)
        self.assertTrue(first['has_more'])
        self.assertTrue(all(e['sequence'] > first['next_after'] for e in second['events']))

    def test_without_job_refused(self):
        with patch.object(safety, 'CURRENT_JOB', None), self.assertRaises(common.WorkerError):
            self.run_tool()

    def test_audit_failure_prevents_execution(self):
        with patch('history.History.event', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_tool()
        self.assertEqual(self.source.read_text(), '[1, 1, 2]')

    def test_destructive_confirmation_still_required(self):
        self.fake.DeleteIdentical_management = self.fake.Deduplicate_management
        self.fake.ListTools = lambda *a: ['DeleteIdentical_management']
        with self.assertRaisesRegex(common.WorkerError, 'confirm=true'):
            self.run_tool(tool='DeleteIdentical_management')
        result = self.run_tool(tool='DeleteIdentical_management', confirm=True)
        self.assertEqual(json.loads(Path(result['primary_output']).read_text()), [1, 2])

    def test_execution_audit_failure_prevents_arcpy_call(self):
        from history import History as WorkerHistory
        original = WorkerHistory.event
        def event(history, job, stage, details):
            if stage == 'executing':
                raise OSError('execution audit failed')
            return original(history, job, stage, details)
        with patch.object(WorkerHistory, 'event', event), patch.object(self.fake, 'Deduplicate_management') as execute:
            with self.assertRaisesRegex(OSError, 'execution audit failed'):
                self.run_tool()
            execute.assert_not_called()
        self.assertEqual(self.source.read_text(), '[1, 1, 2]')

    def test_workspace_parameter_and_multioutput_refused(self):
        workspace = types.SimpleNamespace(name='workspace', datatype='DEWorkspace',
            direction='Input', parameterType='Optional', multiValue=False)
        self.metadata.append(workspace)
        with self.assertRaisesRegex(common.WorkerError, '工作空间'):
            self.run_tool(parameters={**self.args['parameters'], 'workspace': str(self.root)})
        self.metadata.append(types.SimpleNamespace(name='extra_output', datatype='DETable',
            direction='Output', parameterType='Required', multiValue=False))
        with self.assertRaisesRegex(common.WorkerError, '单一显式输出'):
            self.run_tool()

    def test_publication_audit_window_keeps_original_and_retained_result(self):
        from history import History as WorkerHistory
        original = WorkerHistory.event
        def event(history, job, stage, details):
            if stage == 'published':
                raise OSError('audit interrupted after publication')
            return original(history, job, stage, details)
        with patch.object(WorkerHistory, 'event', event):
            with self.assertRaisesRegex(OSError, 'after publication'):
                self.run_tool()
        self.assertEqual(self.source.read_text(), '[1, 1, 2]')
        retained = self.root / 'source_Deduplicate'
        self.assertEqual(json.loads(retained.read_text()), [1, 2])
        manifests = list((self.root / '.arcgis-mcp/transactions').glob('*.json'))
        self.assertEqual(json.loads(manifests[0].read_text())['state'], 'published')
        self.assertEqual(self.history.lineage(retained)['total_steps'], 0)
        self.assertEqual(self.history.events(self.job, limit=100)['events'][-1]['stage'], 'failed')


if __name__ == '__main__':
    unittest.main()
