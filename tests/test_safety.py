"""Regression tests without ArcPy: python -m unittest discover -s tests -p test_safety.py."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
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
from arcgis_mcp.core.history import History
from arcgis_mcp.core.config import Settings
from arcgis_mcp.core.worker import ArcPyWorker, WorkerError, _WorkerDead
from arcgis_mcp.core.jobs import Jobs
from ops import attributes, safety, common


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.safety-unit-', dir=ROOT)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'ARCGIS_MCP_LOCK_DIR': str(self.root / 'locks')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_history_dag_and_branches(self):
        history = History(self.root)
        a, b, c, d = [str(self.root / n) for n in 'ABCD']
        first = history.create('geoprocess', {'operation': 'copy'}, 'session')
        history.update(first, 'succeeded', result={'output': b}, inputs=[a], outputs=[b])
        second = history.create('edit', {'operation': 'calculate_fields'}, 'session')
        history.update(second, 'succeeded', result={'output': c}, inputs=[b], outputs=[c])
        third = history.create('edit', {'operation': 'add_field'}, 'session')
        history.update(third, 'succeeded', result={'output': d}, inputs=[b], outputs=[d])
        self.assertEqual(len(History(self.root).lineage(c)['steps']), 2)
        self.assertEqual(len(history.lineage(b, downstream=True)['steps']), 2)
        self.assertEqual(history.lineage(c)['roots'], ['A'])
        self.assertTrue(history.lineage(c, depth=1)['truncated'])

    def test_failed_operations_do_not_create_lineage(self):
        history = History(self.root)
        job = history.create('edit', {}, 's')
        history.update(job, 'failed', error='bad', inputs=['A'], outputs=['B'])
        self.assertEqual(history.lineage('B')['steps'], [])
        self.assertEqual(history.get(job)['error'], 'bad')

    def test_reused_path_does_not_merge_different_generations(self):
        history = History(self.root)
        paths = {key: str(self.root / key) for key in ('A', 'B', 'C', 'D', 'X')}
        def produce(source, output):
            job = history.create('geoprocess', {}, 's')
            history.update(job, 'succeeded', result={}, inputs=[paths[source]], outputs=[paths[output]])
            return job
        old = produce('A', 'X')
        produce('X', 'B')
        new = produce('C', 'X')
        produce('X', 'D')
        self.assertEqual(history.lineage(paths['B'])['roots'], ['A'])
        self.assertEqual(history.lineage(paths['D'])['roots'], ['C'])
        self.assertEqual(len(history.lineage(paths['X'], downstream=True)['steps']), 1)
        self.assertNotEqual(old, new)

    def test_external_input_has_absolute_identity(self):
        history = History(self.root)
        self.assertEqual(history.display(str(ROOT / 'external.shp')), str(ROOT / 'external.shp'))

    def test_sqlite_parallel_writers(self):
        history = History(self.root)
        errors = []
        def write():
            try:
                for i in range(10):
                    job = History(self.root).create('edit', {'n': i}, 's')
                    history.update(job, 'succeeded', result={}, inputs=['A'], outputs=[job])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=write) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertFalse(errors)
        with history.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 40)

    def test_dead_owner_becomes_unknown(self):
        history = History(self.root)
        job = history.create('edit', {}, 's')
        with history.connect() as db:
            db.execute('UPDATE operations SET owner_pid=? WHERE id=?', (999999999, job))
        self.assertEqual(history.get(job)['state'], 'unknown')

    def test_same_gdb_has_same_lock_across_datasets(self):
        a = safety.WorkspaceLease(str(self.root / 'X.gdb/A'))
        b = safety.WorkspaceLease(str(self.root / 'X.gdb/group/B'))
        c = safety.WorkspaceLease(str(self.root / 'Y.gdb/A'))
        self.assertEqual(a.path, b.path)
        self.assertNotEqual(a.path, c.path)
        with a:
            with self.assertRaisesRegex(safety.WorkerError, 'WorkspaceBusy'):
                with b: pass
            with c: pass
        with b: pass

    def test_workspace_lease_is_held_until_audit_commit(self):
        path = str(self.root / 'X.gdb/A')
        with patch.object(safety, 'DEFER_LEASE_RELEASE', True), patch.object(safety, 'CURRENT_LEASES', {}):
            lease = safety.WorkspaceLease(path)
            try:
                with lease:
                    pass
                self.assertIsNotNone(lease.handle)
                safety.DEFER_LEASE_RELEASE = False
                with self.assertRaisesRegex(safety.WorkerError, 'WorkspaceBusy'):
                    with safety.WorkspaceLease(path):
                        pass
            finally:
                safety.release_leases()
            with safety.WorkspaceLease(path):
                pass

    def test_cross_process_lock(self):
        path = str(self.root / 'X.gdb/A')
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'from ops.safety import WorkspaceLease; '
                'WorkspaceLease(sys.argv[2]).__enter__()')
        with safety.WorkspaceLease(path):
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code,
                str(ROOT / 'src/arcgis_mcp/core'), path], capture_output=True, text=True,
                encoding='utf-8', errors='replace')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WorkspaceBusy', result.stderr)

    def test_publish_preserves_existing_output(self):
        final = self.root / 'result.json'
        final.write_text('old')
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists())
        with patch.dict(sys.modules, {'arcpy': fake}):
            publication = safety.Publication(str(final), overwrite=True, root=self.root)
            Path(publication.work).write_text('new')
            output = publication.publish()
        self.assertEqual(final.read_text(), 'new')
        self.assertEqual(Path(publication.backup).read_text(), 'old')
        self.assertEqual(str(final), output)
        self.assertEqual(Path(output).read_text(), 'new')
        self.assertFalse(safety.internal(output))
        self.assertEqual(json.loads(publication.manifest.read_text())['state'], 'published')

    def test_failed_output_is_quarantined_not_published(self):
        final = str(self.root / 'out.json')
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists())
        with patch.dict(sys.modules, {'arcpy': fake}):
            publication = safety.Publication(final, root=self.root)
            Path(publication.work).write_text('partial')
            recovered = safety.recover(self.root)
        self.assertFalse(Path(final).exists())
        self.assertEqual(len(recovered), 1)
        self.assertEqual(Path(recovered[0]['quarantined']).read_text(), 'partial')

    def test_failed_swap_restores_original(self):
        final = self.root / 'result.json'
        final.write_text('original')
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists())
        original_move = safety.move_dataset
        with patch.dict(sys.modules, {'arcpy': fake}):
            publication = safety.Publication(str(final), overwrite=True, root=self.root)
            Path(publication.work).write_text('new')
            def fail_work(source, destination):
                if source == publication.work:
                    raise OSError('simulated publish failure')
                original_move(source, destination)
            with patch.object(safety, 'move_dataset', side_effect=fail_work):
                with self.assertRaises(OSError):
                    publication.publish()
            self.assertEqual(final.read_text(), 'original')
            self.assertEqual(json.loads(publication.manifest.read_text())['state'], 'rolled_back')

    def test_native_csv_schema_section_follows_rename(self):
        source, output = self.root / 'temporary.csv', self.root / 'final.csv'
        source.write_text('CODE\n001\n')
        schema = self.root / 'schema.ini'
        schema.write_text('[temporary.csv]\nColNameHeader=True\nCol1=CODE Text\n\n[other.csv]\nFormat=CSVDelimited\n')
        with patch.dict(sys.modules, {'arcpy': types.SimpleNamespace(Exists=lambda p: Path(p).exists())}):
            safety.move_dataset(str(source), str(output))
        content = schema.read_text(encoding='utf-8-sig')
        self.assertIn('[final.csv]', content)
        self.assertIn('[other.csv]', content)
        self.assertNotIn('[temporary.csv]', content)

    def test_interrupted_swap_restores_original_on_recovery(self):
        final = self.root / 'result.json'
        final.write_text('original')
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists())
        with patch.dict(sys.modules, {'arcpy': fake}):
            publication = safety.Publication(str(final), overwrite=True, root=self.root)
            Path(publication.work).write_text('partial')
            publication.backup = safety.version_path(str(final))
            publication.stage('backing_up')
            safety.move_dataset(str(final), publication.backup)
            recovered = safety.recover(self.root)
        self.assertEqual(final.read_text(), 'original')
        self.assertEqual(recovered[0]['restored_original'], str(final))

    def test_archive_relocates_existing_history_and_job_results(self):
        history = History(self.root)
        source, target, backup, downstream = [str(self.root / s) for s in ('A', 'D', 'D_history', 'E')]
        old = history.create('geoprocess', {}, 's')
        history.update(old, 'succeeded', result={'output': target}, inputs=[source], outputs=[target])
        child = history.create('geoprocess', {}, 's')
        history.update(child, 'succeeded', result={'output': downstream, 'source': target,
            'in_features': target, 'inputs': [target]}, inputs=[target], outputs=[downstream])
        new = history.create('geoprocess', {}, 's')
        history.update(new, 'succeeded', result={'output': target, 'archived_outputs': [
            {'original_path': target, 'history_path': backup}]}, inputs=[source], outputs=[target])
        self.assertEqual(history.get(old)['result']['output'], backup)
        self.assertEqual(history.get(child)['result']['in_features'], target)
        self.assertEqual(history.get(child)['result']['source'], target)
        self.assertEqual(history.get(child)['result']['inputs'], [backup])
        self.assertEqual(history.lineage(downstream)['steps'][-1]['inputs'], ['D_history'])
        self.assertEqual(len(history.lineage(target)['steps']), 1)

    def test_active_duplicate_submission_reuses_job(self):
        history = History(self.root)
        first, created = history.create('edit', {'target': 'D'}, 'session', reuse=True)
        second, created_again = history.create('edit', {'target': 'D'}, 'session', reuse=True)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)
        history.update(first, 'succeeded', result={})
        third, is_new = history.create('edit', {'target': 'D'}, 'session', reuse=True)
        self.assertTrue(is_new)
        self.assertNotEqual(first, third)

    def test_jobs_do_not_execute_active_duplicate_twice(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def call(op, **args):
            calls.append(op)
            history = History(args['_audit']['root'])
            history.update(args['_audit']['job_id'], 'running')
            started.set()
            release.wait(3)
            history.update(args['_audit']['job_id'], 'succeeded', result={})
            return {}
        jobs = Jobs(types.SimpleNamespace(call=call))
        try:
            first = jobs.submit(self.root, 's', 'edit', {'target': 'D'}, 1, 0, 60)
            self.assertTrue(started.wait(1))
            second = jobs.submit(self.root, 's', 'edit', {'target': 'D'}, 1, 0, 60)
            self.assertEqual(first['job_id'], second['job_id'])
            self.assertTrue(second['deduplicated'])
        finally:
            release.set()
            jobs.queue.join()
            jobs.close()
        self.assertEqual(calls, ['edit'])

    def test_generic_frontend_has_no_synchronous_metadata_preflight(self):
        from arcgis_mcp.core.tool import Services
        from arcgis_mcp.core.project import ProjectRegistry
        from arcgis_mcp.tools.meta import RunArcpyTool
        release = threading.Event()
        calls = []
        settings = Settings(config_path=self.root / 'config.json', forced_project=self.root, job_wait_seconds=0)
        def call(op, **args):
            calls.append(op)
            release.wait(3)
            history = History(args['_audit']['root'])
            history.update(args['_audit']['job_id'], 'succeeded', result={})
            return {}
        worker = types.SimpleNamespace(call=call, _jobs_lock=threading.Lock())
        tool = RunArcpyTool(Services(settings, ProjectRegistry(settings), worker))
        try:
            result = json.loads(tool.run_arcpy_tool(types.SimpleNamespace(headers={}),
                'Dissolve_management', {'in_features': './A.gdb/A', 'out_feature_class': './A.gdb/B'}))
            self.assertTrue(result['pending'])
            self.assertFalse(release.is_set())
        finally:
            release.set()
            worker.jobs.queue.join()
            worker.jobs.close()
        self.assertEqual(calls, ['run_arcpy_tool'])

    def test_localized_value_table_paths_resolve_against_project(self):
        from ops import meta
        parameters = [types.SimpleNamespace(name='in_features', datatype='值表', direction='Input',
                        parameterType='Required', multiValue=False),
                      types.SimpleNamespace(name='out_feature_class', datatype='要素类', direction='Output',
                        parameterType='Required', multiValue=False)]
        source = self.root / 'A.gdb' / 'one'
        source.parent.mkdir()
        source.write_text('source')
        seen = {}
        def intersect(**args):
            seen.update(args)
            Path(args['out_feature_class']).write_text('{}')
        fake = types.SimpleNamespace(GetParameterInfo=lambda name: parameters,
            Intersect_analysis=intersect, Exists=lambda p: Path(p).exists())
        with patch.dict(sys.modules, {'arcpy': fake}), patch.object(safety, 'CURRENT_ROOT', str(self.root)), \
             patch.object(meta, 'describe_dataset', return_value={}), \
             patch.object(safety, 'move_dataset', side_effect=lambda a, b: os.rename(a, b)):
            fake.EnvManager = lambda **args: contextlib.nullcontext()
            result = meta.op_run_arcpy_tool('Intersect_analysis', parameters={
                'in_features': [['./A.gdb/one', 1]], 'out_feature_class': './out.json'})
        self.assertEqual(seen['in_features'], [[str(source), 1]])
        self.assertEqual(result['inputs'], [str(source)])

    def test_catalog_separates_discovery_from_adapter_eligibility(self):
        from ops import meta
        names = ['Buffer_analysis', 'GetCount_management', 'Topology_unclassified']
        fake = types.SimpleNamespace(ListTools=lambda pattern: names)
        with patch.dict(sys.modules, {'arcpy': fake}):
            result = meta.op_list_arcpy_tools(limit=1, with_syntax=False)
            full = meta.op_list_arcpy_tools(limit=10, with_syntax=False)
        self.assertEqual((result['total_matched'], result['classified_matched'], result['returned']), (3, 2, 1))
        self.assertEqual(full['tools'][0]['execution']['mode'], 'produce_publish')
        self.assertEqual(full['tools'][1]['execution']['mode'], 'read')
        self.assertFalse(full['tools'][2]['execution']['allowed'])
        self.assertEqual(full['tools'][2]['execution']['status'], 'unsupported')

    def test_describe_reports_edit_parameter_restrictions_and_unknown_support(self):
        from ops import meta
        parameters = [types.SimpleNamespace(name='in_table', direction='Input',
            parameterType='Required', datatype='Table View', multiValue=False)]
        fake = types.SimpleNamespace(GetParameterInfo=lambda name: parameters,
            CalculateField_management=lambda: None, Unknown_management=lambda: None)
        with patch.dict(sys.modules, {'arcpy': fake}):
            edit = meta.op_describe_arcpy_tool('CalculateField_management', include_usage=False)
            unknown = meta.op_describe_arcpy_tool('Unknown_management', include_usage=False)
        support = edit['execution']
        self.assertTrue(support['allowed'])
        self.assertEqual(support['mode'], 'copy_edit')
        self.assertIn('expression', support['supported_parameters'])
        self.assertNotIn('enforce_domains', support['supported_parameters'])
        self.assertEqual(support['validation'], 'not_certified_for_all_parameters')
        self.assertFalse(unknown['execution']['allowed'])
        self.assertEqual(len(unknown['parameters']), 1)

    def test_unclassified_execution_rejected_even_with_confirm(self):
        from ops import meta
        calls = []
        fake = types.SimpleNamespace(Unknown_management=lambda **kwargs: calls.append(kwargs))
        with patch.dict(sys.modules, {'arcpy': fake}):
            with self.assertRaisesRegex(common.WorkerError, 'confirm=true'):
                meta.op_run_arcpy_tool('Unknown_management', parameters={'in_data': 'A'}, confirm=True)
        self.assertEqual(calls, [])

    def test_execution_support_covers_all_registered_adapters(self):
        from ops import meta
        modes = [(meta.SAFE_PRODUCERS, 'produce_publish'), (meta.EDIT_ADAPTERS, 'copy_edit'),
                 (meta.CREATE_ADAPTERS, 'create_publish'), (meta.READ_ADAPTERS, 'read'),
                 ({'delete_management'}, 'archive_delete')]
        for names, mode in modes:
            for name in names:
                with self.subTest(name=name):
                    support = meta.tool_execution_support(name.upper())
                    self.assertTrue(support['allowed'])
                    self.assertEqual(support['mode'], mode)

    def test_published_data_never_cleaned(self):
        final = str(self.root / 'out.json')
        fake = types.SimpleNamespace(Exists=lambda p: Path(p).exists())
        with patch.dict(sys.modules, {'arcpy': fake}):
            p = safety.Publication(final, root=self.root)
            Path(p.work).write_text('valid')
            p.publish()
            self.assertEqual(safety.recover(self.root), [])
        self.assertEqual(Path(final).read_text(), 'valid')

    def test_calculate_where_targets_filtered_view(self):
        calls = []
        management = types.SimpleNamespace(
            MakeFeatureLayer=lambda *args: calls.append(('view', args)),
            CalculateField=lambda *args: calls.append(('calculate', args)),
            GetCount=lambda p: ['1' if str(p).startswith('mcp_calc') else '100'],
            Delete=lambda p: calls.append(('delete', p)))
        fake = types.SimpleNamespace(management=management,
                                     Describe=lambda p: types.SimpleNamespace(dataType='FeatureClass'))
        with patch.dict(sys.modules, {'arcpy': fake}), patch.object(attributes, 'field_names', return_value=['V']):
            result = attributes._calculate_fields('source', {'assignments': {'V': '7'}, 'where': 'OID=1'})
        self.assertEqual(calls[0][1][2], 'OID=1')
        self.assertEqual(calls[1][1][0], calls[0][1][1])
        self.assertEqual(result['updated_count'], 1)
        self.assertEqual(calls[-1][0], 'delete')

    def test_write_crash_is_not_retried(self):
        worker = ArcPyWorker(Settings(config_path=self.root / 'config.json'))
        worker._proc = types.SimpleNamespace(poll=lambda: None)
        worker._ready = True
        with patch.object(worker, '_call_locked', side_effect=_WorkerDead('crash')) as call, \
             patch.object(worker, '_stop_locked'):
            with self.assertRaisesRegex(WorkerError, '禁止自动重试'):
                worker.call('edit')
        self.assertEqual(call.call_count, 1)

    def test_queue_wait_is_bounded(self):
        worker = ArcPyWorker(Settings(config_path=self.root / 'config.json'))
        acquired, release = threading.Event(), threading.Event()
        def hold():
            with worker._lock:
                acquired.set()
                release.wait(3)
        thread = threading.Thread(target=hold)
        thread.start()
        acquired.wait(1)
        try:
            with self.assertRaisesRegex(WorkerError, 'WorkerBusy'):
                worker.call('read', queue_timeout=0.01)
        finally:
            release.set()
            thread.join()

    def test_soft_wait_keeps_late_result(self):
        class FakeWorker:
            def call(self, op, **args):
                history = History(args['_audit']['root'])
                job = args['_audit']['job_id']
                history.update(job, 'running')
                time.sleep(0.05)
                result = {'output': str(history.root / 'new.shp')}
                history.update(job, 'succeeded', result=result, inputs=['old'], outputs=[result['output']])
                return result
        jobs = Jobs(FakeWorker())
        result = jobs.submit(self.root, 'session', 'edit', {}, 1, 0, 2)
        self.assertTrue(result['pending'])
        jobs.queue.join()
        saved = History(self.root).get(result['job_id'])
        self.assertEqual(saved['state'], 'succeeded')
        self.assertTrue(saved['result']['output'].endswith('new.shp'))
        jobs.close()

    def test_hard_limit_terminates_worker(self):
        import io
        worker = ArcPyWorker(Settings(config_path=self.root / 'config.json'))
        worker._proc = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
        with patch.object(worker, '_stop_locked') as stop:
            with self.assertRaisesRegex(WorkerError, '硬执行上限'):
                worker._call_locked('edit', {}, 0.01)
        stop.assert_called_once()

    def test_lock_error_is_actionable(self):
        error = safety.lock_error(RuntimeError('ERROR 000464: Cannot get exclusive schema lock'))
        self.assertIsInstance(error, safety.WorkerError)
        self.assertIn('ArcGIS Pro', str(error))

    def test_expired_job_is_not_executed(self):
        worker = types.SimpleNamespace(call=lambda *a, **kw: self.fail('expired job executed'))
        jobs = Jobs(worker)
        result = jobs.submit(self.root, 's', 'edit', {}, 1, 0.1, -1)
        self.assertEqual(result['job_state'], 'failed')
        jobs.close()

    def test_queued_deadline_observable_while_worker_is_busy(self):
        history = History(self.root)
        job = history.create('edit', {}, 's', queue_timeout=-1)
        self.assertEqual(history.get(job)['state'], 'failed')

    def test_startup_is_not_queue_time(self):
        history = History(self.root)
        job = history.create('edit', {}, 's', queue_timeout=0.01)
        history.update(job, 'starting')
        time.sleep(0.02)
        self.assertEqual(history.get(job)['state'], 'starting')

    def test_plain_file_exists_when_arcpy_cache_says_false(self):
        target = self.root / 'result.geojson'
        target.write_text('{}')
        with patch.dict(sys.modules, {'arcpy': types.SimpleNamespace(Exists=lambda p: False)}):
            self.assertTrue(common.path_exists(str(target)))


if __name__ == '__main__':
    unittest.main()
