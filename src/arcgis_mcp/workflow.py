"""CLI using exactly the same submission, worker, SDK and history as MCP."""
import argparse
import json
from pathlib import Path
import time
from types import SimpleNamespace

from .core.config import load_settings
from .core.history import History
from .core.project import ProjectRegistry
from .core.tool import Services
from .core.worker import ArcPyWorker
from .tools.workflow import RunWorkflow


def main():
    parser = argparse.ArgumentParser(description='Run an audited ArcPy workflow (trusted local scripts).')
    parser.add_argument('script')
    parser.add_argument('--project', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--inputs', required=True, help='JSON file: alias -> input dataset path')
    parser.add_argument('--parameters', help='JSON file with workflow parameters')
    parser.add_argument('--output-gdb')
    parser.add_argument('--input-mode', choices=('snapshot', 'read_only'), default='snapshot',
                        help='read_only references require trusted code and deployed read-only permissions')
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--confirm', action='store_true', required=True)
    args = parser.parse_args()
    settings = load_settings()
    settings.forced_project = Path(args.project).resolve()
    worker = ArcPyWorker(settings)
    try:
        service = Services(settings, ProjectRegistry(settings), worker)
        result = json.loads(RunWorkflow(service).run_workflow(SimpleNamespace(headers={}),
            args.script, args.reason, json.loads(Path(args.inputs).read_text(encoding='utf-8-sig')),
            json.loads(Path(args.parameters).read_text(encoding='utf-8-sig')) if args.parameters else {},
            args.output_gdb, args.timeout, args.confirm, args.input_mode))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if 'job_id' not in result:
            return 1
        history = History(settings.forced_project)
        while True:
            result = history.get(result['job_id'])
            if result['state'] not in ('queued', 'starting', 'running'):
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return 0 if result['state'] == 'succeeded' else 1
            time.sleep(0.5)
    finally:
        worker.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
