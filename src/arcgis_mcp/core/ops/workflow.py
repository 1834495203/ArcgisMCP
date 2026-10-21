"""Launch a fresh ArcPy child, preserving the existing worker protocol and job audit."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from . import safety
from .common import WorkerError


def run_workflow(code, script_path, reason, inputs, parameters, output_gdb,
                 timeout_seconds=3600, confirm=False, layout=None, mode='final', unit=None,
                 input_mode='snapshot', graph=None):
    from history import History
    if not safety.CURRENT_ROOT or not safety.CURRENT_JOB:
        raise WorkerError('工作流必须通过任务和审计入口运行。')
    if not confirm or not isinstance(reason, str) or not reason.strip():
        raise WorkerError('工作流需要 confirm=true 和非空 reason。')
    if not isinstance(timeout_seconds, int) or not 10 <= timeout_seconds <= 86400:
        raise WorkerError('timeout_seconds 必须在 10~86400 秒之间。')
    if input_mode not in ('snapshot', 'read_only'):
        raise WorkerError('input_mode 必须为 snapshot/read_only。')
    root, job = Path(safety.CURRENT_ROOT).resolve(), safety.CURRENT_JOB
    if unit:
        from workflow_plan import WorkflowPlan
        try:
            WorkflowPlan(root).register(job, unit)
        except ValueError as exc:
            raise WorkerError(str(exc)) from exc
    folder = root / '.arcgis-mcp' / 'workflows' / job
    folder.mkdir(parents=True, exist_ok=False)
    if unit:
        (folder / 'body.py').write_text(unit['code'], encoding='utf-8')
        (folder / 'step.json').write_text(json.dumps(unit, ensure_ascii=False, indent=2), encoding='utf-8')
    if graph:
        (folder / 'graph.json').write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding='utf-8')
    digest = hashlib.sha256(code.encode('utf-8')).hexdigest()
    script = folder / 'script.py'
    script.write_text(code, encoding='utf-8')
    request = dict(root=str(root), job_id=job, script=str(script), source_script=script_path,
        script_sha256=digest, reason=reason, inputs=inputs, parameters=parameters,
        output_gdb=output_gdb, auto_create_gdb=safety.AUTO_CREATE_GDB,
        parent_pid=os.getpid(), timeout_seconds=timeout_seconds, layout=layout, mode=mode,
        workflow_id=(unit or graph or {}).get('workflow_id'), input_mode=input_mode,
        graph=graph)
    manifest = folder / 'request.json'
    manifest.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    history = History(root)
    history.event(job, 'workflow_snapshot', {'script': str(script), 'sha256': digest,
        'reason': reason, 'inputs': inputs, 'parameters': parameters})
    runtime = Path(__file__).resolve().parents[1] / 'workflow_runtime.py'
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    # Native ArcPy output also has a durable home, separate from protocol stdout.
    with open(folder / 'stdout.log', 'w', encoding='utf-8') as stdout, open(
            folder / 'stderr.log', 'w', encoding='utf-8') as stderr:
        try:
            child = subprocess.run([sys.executable, '-X', 'utf8', str(runtime), str(manifest)],
                cwd=str(root), env=env, stdout=stdout, stderr=stderr, timeout=timeout_seconds,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired as exc:
            history.event(job, 'workflow_timeout', {'timeout_seconds': timeout_seconds,
                'artifacts': str(folder), 'note': '子进程已终止；检查成功步骤和未确认事务，不要自动重放。'})
            raise TimeoutError('工作流执行超时，子进程已终止；发布结果可能未知，请查询 job_logs。') from exc
    outcome = folder / 'outcome.json'
    if child.returncode != 0 or not outcome.exists():
        details = json.loads(outcome.read_text(encoding='utf-8')) if outcome.exists() else {}
        history.event(job, 'workflow_failed', {'exit_code': child.returncode, 'artifacts': str(folder), **details})
        if not outcome.exists():
            raise RuntimeError('工作流子进程未提交结果，执行状态未知；检查成功步骤及事务日志。')
        if any(history.get(step_id)['state'] == 'unknown' for step_id in details.get('steps', [])):
            raise RuntimeError('工作流有发布或审计未确认的步骤；结果未知，禁止自动重放。')
        raise WorkerError('工作流失败；已完成步骤保留：' + str(details.get('error',
            '子进程退出且未提交结果；检查阶段日志与事务清单。')))
    result = json.loads(outcome.read_text(encoding='utf-8'))
    history.event(job, 'workflow_finished', {'steps': result['steps'], 'outputs': result['outputs']})
    return {**result, 'script_sha256': digest, 'script_snapshot': str(script), 'artifacts': str(folder),
            'reason': reason, 'parameters': parameters,
            'note': '工作流成功表示登记步骤完成，不代表业务验收；此入口不是文件系统沙箱。'}
