"""Execution contracts only: no business-area or expected-merge thresholds."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'src/arcgis_mcp/core'))
from arcgis_mcp.core.config import Settings, load_settings, load_project_config, ConfigError
from arcgis_mcp.core.history import History
from arcgis_mcp.core.project import ProjectRegistry
from arcgis_mcp.core.resolver import resolve_output_path, resolve_input_path
from arcgis_mcp.core.tool import Services, Tool
from arcgis_mcp.tools.history import JobResult, Explain
from ops import safety, common, tabular, raster


class ExecutionContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.safety-contract-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(config_path=self.root / 'config.json', forced_project=self.root)
        self.projects = ProjectRegistry(self.settings)
        self.services = Services(self.settings, self.projects, types.SimpleNamespace())
        self.ctx = types.SimpleNamespace(headers={})

    def test_project_output_policy_and_explicit_paths(self):
        folder = self.root / '.arcgis-mcp'
        folder.mkdir()
        (folder / 'datasources.json').write_text(json.dumps({
            'output_workspace': './work.gdb', 'auto_create_gdb': False}))
        view = self.projects.view()
        self.assertEqual(view.settings.output_workspace, self.root / 'work.gdb')
        self.assertFalse(view.settings.auto_create_gdb)
        self.assertEqual(resolve_output_path('step', view.settings), self.root / 'work.gdb/step')
        self.assertEqual(resolve_output_path('./other.gdb/step', view.settings), self.root / 'other.gdb/step')
        self.assertEqual(resolve_output_path('./stats.csv', view.settings), self.root / 'stats.csv')
        self.assertEqual(resolve_input_path('step', view.settings), self.root / 'work.gdb/step')

    def test_session_output_choice_does_not_leak_across_clients(self):
        self.projects.set_output_policy('a', self.root, './A.gdb', False)
        self.projects.set_output_policy('b', self.root, './B.gdb', True)
        self.assertEqual(self.projects.view(session_key='a').settings.output_workspace.name, 'A.gdb')
        self.assertEqual(self.projects.view(session_key='b').settings.output_workspace.name, 'B.gdb')
        self.assertIsNone(self.projects.view(session_key='c').settings.output_workspace)
        self.assertFalse(self.projects.view(session_key='a').settings.auto_create_gdb)

    def test_global_output_policy_rejects_invalid_configuration(self):
        path = self.root / 'config.json'
        for data in ({'output_workspace': './x.shp'}, {'auto_create_gdb': 'false'}):
            path.write_text(json.dumps(data))
            with self.assertRaises(ConfigError):
                load_settings(path)
        path.write_text(json.dumps({'output_workspace':'./wrong.csv', 'auto_create_gdb':False}))
        project = load_project_config(path, self.root)
        self.assertFalse(project.auto_create_gdb)
        self.assertTrue(project.problems)

    def test_disabled_automatic_gdb_creation_has_no_side_effect(self):
        calls = []
        fake = types.SimpleNamespace(Exists=lambda p: False,
            management=types.SimpleNamespace(CreateFileGDB=lambda *args: calls.append(args)))
        with patch.dict(sys.modules, {'arcpy': fake}), patch.object(safety, 'AUTO_CREATE_GDB', False):
            with self.assertRaisesRegex(common.WorkerError, 'auto_create_gdb=false'):
                common.ensure_output_container(str(self.root / 'missing.gdb/step'))
        self.assertEqual(calls, [])
        self.assertFalse((self.root / 'missing.gdb').exists())

    def test_missing_feature_dataset_is_not_created_as_a_folder(self):
        gdb = self.root / 'existing.gdb'
        gdb.mkdir()
        with patch.dict(sys.modules, {'arcpy': types.SimpleNamespace(Exists=lambda p: Path(p).exists())}):
            with self.assertRaisesRegex(common.WorkerError, '要素数据集不存在'):
                common.ensure_output_container(str(gdb / 'group/step'))
        self.assertFalse((gdb / 'group').exists())
        with patch.dict(sys.modules, {'arcpy': types.SimpleNamespace(Exists=lambda p: Path(p).exists())}):
            with self.assertRaisesRegex(common.WorkerError, 'GDB 外'):
                common.ensure_output_container(str(gdb / 'result.csv'))

    def test_generic_output_uses_selected_workspace(self):
        from ops import meta
        gdb = self.root / 'work.gdb'
        gdb.mkdir()
        (self.root/'source').write_text('source')
        metadata = [types.SimpleNamespace(name='in_features', datatype='Feature Layer', direction='Input', parameterType='Required'),
                    types.SimpleNamespace(name='out_feature_class', datatype='Feature Class', direction='Output', parameterType='Required')]
        fake = types.SimpleNamespace(GetParameterInfo=lambda name: metadata,
            CopyFeatures_management=lambda **kwargs: Path(kwargs['out_feature_class']).write_text('copy'),
            EnvManager=lambda **kwargs: __import__('contextlib').nullcontext(), Exists=lambda p: Path(p).exists())
        with patch.dict(sys.modules, {'arcpy': fake}), patch.object(safety, 'CURRENT_ROOT', str(self.root)), \
             patch.object(safety, 'OUTPUT_WORKSPACE', str(gdb)), patch.object(meta, 'describe_dataset', return_value={}), \
             patch.object(safety, 'move_dataset', side_effect=lambda source, target: os.rename(source, target)):
            result = meta.op_run_arcpy_tool('CopyFeatures_management', parameters={
                'in_features':'./source', 'out_feature_class':'copied'})
        self.assertEqual(Path(result['outputs'][0]['path']), gdb / 'copied')
        self.assertEqual((self.root/'source').read_text(), 'source')

    def test_compact_lineage_is_paged_and_full_result_remains_available(self):
        history = History(self.root)
        source = str(self.root / 'A')
        for name in ('B', 'C', 'D'):
            output = str(self.root / name)
            job = history.create('geoprocess', {'operation': 'copy'}, 's')
            history.update(job, 'succeeded', result={'output': output,
                'params': {'option': name}, 'fields': [{'name': 'VALUE', 'type': 'Integer', 'extra': 'x'*5000}]},
                inputs=[source], outputs=[output])
            source = output
        page = history.lineage(source, detail=False, limit=2)
        self.assertEqual((page['total_steps'], page['returned'], page['next_offset']), (3, 2, 2))
        self.assertNotIn('result', page['steps'][0])
        self.assertEqual(page['steps'][0]['params'], {'option': 'B'})
        self.assertLess(len(json.dumps(page)), 4000)
        last = history.lineage(source, detail=False, offset=2, limit=2)
        self.assertIsNone(last['next_offset'])
        self.assertEqual(last['returned'], 1)
        self.assertIn('fields', history.get(job)['result'])
        explained = json.loads(Explain(self.services).explain(self.ctx, source, limit=2))
        self.assertIn('copy', explained['explanation'])
        self.assertEqual(explained['next_offset'], 2)

    def test_job_wait_finishes_without_calling_worker_and_compacts_only_schema(self):
        history = History(self.root)
        job = history.create('edit', {'operation': 'example'}, 's')
        def finish():
            time.sleep(0.1)
            history.update(job, 'succeeded', result={'output': 'B', 'updated_count': 0,
                'fields': [{'name': 'VALUE', 'type': 'Integer', 'precision': 5}]})
        thread = threading.Thread(target=finish)
        thread.start()
        try:
            result = json.loads(JobResult(self.services).job_result(self.ctx, job, wait_seconds=1))
        finally:
            thread.join()
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['result']['updated_count'], 0)
        self.assertNotIn('args', result)
        self.assertNotIn('precision', result['result']['fields'][0])
        full = json.loads(JobResult(self.services).job_result(self.ctx, job, detail=True))
        self.assertIn('precision', full['result']['fields'][0])
        self.assertIn('args', full)

    def test_job_wait_timeout_does_not_fail_or_resubmit_task(self):
        history = History(self.root)
        job = history.create('edit', {}, 's')
        result = json.loads(JobResult(self.services).job_result(self.ctx, job, wait_seconds=0.02))
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(history.get(job)['state'], 'queued')

    def test_csv_cursor_references_released_before_workspace_cleanup(self):
        active = set()
        observed = []
        class Cursor:
            def __init__(self, *args): active.add(id(self))
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __iter__(self): return iter([(7,)])
            def __del__(self): active.discard(id(self))
        def delete(gdb):
            observed.append(len(active))
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists(),
            ListFields=lambda p: [types.SimpleNamespace(name='VALUE', type='Integer')],
            da=types.SimpleNamespace(SearchCursor=Cursor),
            management=types.SimpleNamespace(CreateFileGDB=lambda folder, name: (Path(folder)/name).mkdir(),
                Delete=delete, ClearWorkspaceCache=lambda p: None))
        with patch.dict(sys.modules, {'arcpy': fake}), patch.object(safety, 'CURRENT_ROOT', str(self.root)):
            output = self.root / 'stats.csv'
            with tabular.table_output(str(output)) as table:
                Path(table).write_text('table')
        self.assertEqual(output.read_text(encoding='utf-8-sig'), 'VALUE\n7\n')
        self.assertEqual(observed, [0], 'cursor reference still holds workspace at deletion')
        self.assertEqual(list((self.root / '.arcgis-mcp/scratch').iterdir()), [])

    def test_failed_scratch_cleanup_keeps_retry_marker(self):
        directory = self.root / 'private'
        directory.mkdir()
        (directory / tabular.MARKER).write_text(json.dumps({'kind': 'csv_scratch_v1'}))
        (directory / 'export.gdb').mkdir()
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists(),
            management=types.SimpleNamespace(Delete=lambda p: None, ClearWorkspaceCache=lambda p: None))
        with patch.dict(sys.modules, {'arcpy': fake}), patch.object(tabular.shutil, 'rmtree', side_effect=PermissionError('locked')):
            result = tabular._remove_scratch(directory)
        self.assertIn('locked', result['error'])
        self.assertEqual(json.loads((directory/tabular.MARKER).read_text())['kind'], 'csv_scratch_v1')

    def test_scratch_retry_does_not_touch_unmarked_or_live_workspaces(self):
        base = self.root / '.arcgis-mcp/scratch'
        unmarked = base / 'mcpwork_csv_unmarked/export.gdb'
        unmarked.mkdir(parents=True)
        owned = base / 'mcpwork_csv_owned'
        owned.mkdir()
        (owned / tabular.MARKER).write_text(json.dumps({'kind': 'csv_scratch_v1'}))
        (owned / 'export.gdb').mkdir()
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists(),
            management=types.SimpleNamespace(Delete=lambda p: None, ClearWorkspaceCache=lambda p: None))
        with patch.dict(sys.modules, {'arcpy': fake}):
            with safety.WorkspaceLease(str(owned / 'export.gdb')):
                self.assertEqual(tabular.cleanup_scratch(self.root)['removed'], 0)
            self.assertEqual(tabular.cleanup_scratch(self.root)['removed'], 1)
        self.assertTrue(unmarked.exists())
        self.assertFalse(owned.exists())

    def test_raster_to_polygon_reports_actual_field_without_business_checks(self):
        calls = []
        fake = types.SimpleNamespace(conversion=types.SimpleNamespace(RasterToPolygon=lambda *a: calls.append(a)),
            ListFields=lambda p: [types.SimpleNamespace(name='OBJECTID'), types.SimpleNamespace(name='gridcode')])
        with patch.dict(sys.modules, {'arcpy': fake}):
            result = raster._raster_to_polygon(['A'], 'B', {'raster_field': 'Value', 'simplify': False})
        self.assertEqual(result['output_value_field'], 'gridcode')
        self.assertEqual(calls[0][2], 'NO_SIMPLIFY')
        self.assertNotIn('validation_status', result)


if __name__ == '__main__':
    unittest.main()
