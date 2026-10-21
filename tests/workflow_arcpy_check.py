"""Real workflow subprocess/SDK integration, only synthetic data in a fresh project."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from arcgis_mcp.core.config import load_settings
from arcgis_mcp.core.history import History
from arcgis_mcp.core.history import process_alive
from arcgis_mcp.core.project import ProjectRegistry
from arcgis_mcp.core.tool import Services, Tool
from arcgis_mcp.core.worker import ArcPyWorker
from arcgis_mcp.tools.workflow import RunWorkflow


class Probe(Tool):
    name = 'workflow_probe'
    def workflow_probe(self):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix='.safety-workflow-real-', dir=ROOT) as folder:
        root = Path(folder)
        settings = load_settings()
        settings.forced_project = root
        settings.queue_timeout = 180
        worker = ArcPyWorker(settings)
        services = Services(settings, ProjectRegistry(settings), worker)
        tool = RunWorkflow(services)
        ctx = SimpleNamespace(headers={})
        history = History(root)
        def await_job(result, state='succeeded'):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                snapshot = history.get(result['job_id'])
                if snapshot['state'] not in ('queued', 'starting', 'running'):
                    assert snapshot['state'] == state, snapshot
                    return snapshot
                time.sleep(0.1)
            raise AssertionError('workflow did not finish')
        def check(name, condition):
            assert condition, name
            print('PASS', name, flush=True)
        try:
            seed = root / 'seed.py'
            seed.write_text("import arcpy,sys\nr=sys.argv[1]\n"
                "arcpy.management.CreateFileGDB(r,'source.gdb')\n"
                "p=r+'/source.gdb/original'\n"
                "arcpy.management.CreateFeatureclass(r+'/source.gdb','original','POINT',spatial_reference=4326)\n"
                "arcpy.management.AddField(p,'VALUE','LONG')\n"
                "with arcpy.da.InsertCursor(p,['SHAPE@XY','VALUE']) as c:\n"
                " c.insertRow([(110,30),1])\n c.insertRow([(111,31),2])\n", encoding='utf-8')
            subprocess.run([settings.arcpy_python, str(seed), str(root)], check=True)
            script = root / 'process.py'
            code = """import arcpy,sys
source = workflow.inputs['features']
with workflow.step('select', [source], {'where': workflow.parameters['where']}) as selection:
    arcpy.analysis.Select(source, selection.output(), workflow.parameters['where'])
with workflow.step('calculate', [selection.result], {'value': 7}) as calculation:
    edited = calculation.edit_copy(selection.result)
    arcpy.management.CalculateField(edited, 'VALUE', '7', 'PYTHON3')
print('workflow stdout')
print('workflow stderr', file=sys.stderr)
workflow.set_outputs({'final': calculation.result})
"""
            script.write_text(code, encoding='utf-8')
            submitted = json.loads(tool.run_workflow(ctx, str(script), '验证脚本审计与条件处理',
                {'features': './source.gdb/original'}, {'where': 'VALUE = 1'}, confirm=True))
            # Submission must execute exactly the captured content, not the subsequently edited file.
            script.write_text("raise RuntimeError('must not run changed source')", encoding='utf-8')
            success = await_job(submitted)['result']
            final = success['outputs'][0]['path']
            def values(path):
                return [r['VALUE'] for r in worker.call('read', workspace=str(Path(path).parent),
                    dataset=Path(path).name, fields=['VALUE'], limit=10)['records']]
            check('original content unchanged', values(str(root / 'source.gdb/original')) == [1, 2])
            check('script result content correct', values(final) == [7])
            check('submitted script is immutable snapshot', success['script_sha256'] == hashlib.sha256(code.encode()).hexdigest())
            chain = history.lineage(final, detail=False)['steps']
            check('lineage contains snapshot and both real steps', [s['operation'] for s in chain] == ['snapshot_features', 'select', 'calculate'])
            check('parent summary does not hide step DAG', all(s['parent_job_id'] == submitted['job_id'] for s in chain))
            check('intermediate content retained', values(history.get(success['steps'][1])['result']['outputs'][0]['path']) == [1])
            events = history.events(submitted['job_id'], limit=100)['events']
            check('stdout stderr and environments persisted', {'stdout', 'stderr', 'workflow_environment'} <= {e['stage'] for e in events})
            bad = root / 'fail.py'
            bad.write_text("""import arcpy
source = workflow.inputs['features']
with workflow.step('keep', [source]) as keep:
    arcpy.management.CopyFeatures(source, keep.output())
with workflow.step('fail', [keep.result]) as failed:
    arcpy.management.CalculateField(failed.edit_copy(keep.result), 'VALUE', '99', 'PYTHON3')
    raise RuntimeError('intentional workflow failure')
""", encoding='utf-8')
            failed_job = json.loads(tool.run_workflow(ctx, str(bad), '验证失败保留',
                {'features': './source.gdb/original'}, confirm=True))
            await_job(failed_job, 'failed')
            logs = history.events(failed_job['job_id'], limit=100)['events']
            steps = [e['details']['job_id'] for e in logs if e['stage'] == 'step_started']
            check('failure preserves completed steps', [history.get(j)['state'] for j in steps] == ['succeeded', 'succeeded', 'failed'])
            check('failed edit changes neither original nor intermediate', values(str(root / 'source.gdb/original')) == [1, 2]
                and values(history.get(steps[1])['result']['outputs'][0]['path']) == [1, 2])
            check('failed script remains auditable after reopening history', any(e['stage'] == 'script_failed'
                for e in History(root).events(failed_job['job_id'], limit=100)['events']))
            script.write_text(code, encoding='utf-8')
            inputs_file = root / 'inputs.json'
            inputs_file.write_text(json.dumps({'features': './source.gdb/original'}), encoding='utf-8')
            params_file = root / 'parameters.json'
            params_file.write_text(json.dumps({'where': 'VALUE = 1'}), encoding='utf-8')
            cli = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'arcgis_mcp.workflow', str(script),
                '--project', str(root), '--reason', '验证 CLI 复跑', '--inputs', str(inputs_file),
                '--parameters', str(params_file), '--confirm'], cwd=ROOT,
                capture_output=True, text=True, encoding='utf-8', timeout=180)
            assert cli.returncode == 0, (cli.stdout, cli.stderr)
            replay = json.loads(cli.stdout.strip().splitlines()[-1])
            check('CLI replay uses same durable pipeline and preserves earlier outputs',
                values(replay['result']['outputs'][0]['path']) == [7] and values(final) == [7])
            slow = root / 'slow.py'
            slow.write_text("import time\nprint('started')\ntime.sleep(60)\n", encoding='utf-8')
            timed = json.loads(tool.run_workflow(ctx, str(slow), '验证子进程超时终止', {},
                timeout_seconds=10, confirm=True))
            await_job(timed, 'unknown')
            timeout_logs = History(root).events(timed['job_id'], limit=100)['events']
            pids = [e['details']['pid'] for e in timeout_logs if e['stage'] == 'workflow_environment']
            check('timeout is unknown and the script child has exited',
                any(e['stage'] == 'workflow_timeout' for e in timeout_logs) and all(not process_alive(p) for p in pids))
            check('worker still reads original after script timeout', values(str(root / 'source.gdb/original')) == [1, 2])
        finally:
            worker.shutdown()
    print('PASS real ArcPy workflow checks')


if __name__ == '__main__':
    main()
