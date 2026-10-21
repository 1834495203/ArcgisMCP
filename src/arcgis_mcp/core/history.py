"""Durable logical operations and jobs; standard library only (also used by Python 3.9 worker)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
import contextlib
import hashlib
from pathlib import Path

DATA_POLICY_VERSION = 'archive-replace-4'


def canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def process_alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of death.
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class History:
    def __init__(self, root):
        self.root = Path(root).resolve()
        folder = self.root / '.arcgis-mcp'
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / 'history.sqlite3'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, session TEXT, op TEXT, args TEXT,
                state TEXT, created REAL, started REAL, finished REAL,
                result TEXT, error TEXT, owner_pid INTEGER, worker_pid INTEGER)''')
            db.execute('''CREATE TABLE IF NOT EXISTS edges (
                operation_id TEXT, output TEXT, input TEXT,
                PRIMARY KEY(operation_id, output, input))''')
            db.execute('CREATE INDEX IF NOT EXISTS edges_output ON edges(output)')
            db.execute('CREATE INDEX IF NOT EXISTS edges_input ON edges(input)')
            db.execute('CREATE TABLE IF NOT EXISTS datasets (key TEXT PRIMARY KEY, path TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS job_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                ts REAL NOT NULL, stage TEXT NOT NULL, details TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id,sequence)')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(operations)')}
            for column in ('expires', 'fingerprint'):
                if column in columns:
                    continue
                try:
                    db.execute('ALTER TABLE operations ADD COLUMN ' + column +
                               (' REAL' if column == 'expires' else ' TEXT'))
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc).lower():
                        raise
            db.execute('CREATE INDEX IF NOT EXISTS operations_fingerprint ON operations(fingerprint,state)')

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, op, args, session, queue_timeout=60, reuse=False):
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            fingerprint = hashlib.sha256(json.dumps([session, op, args], sort_keys=True,
                ensure_ascii=False, default=str).encode('utf-8')).hexdigest()
            if reuse:
                existing = db.execute("SELECT id FROM operations WHERE fingerprint=? AND "
                    "(state IN ('starting','running') OR (state='queued' AND expires>?)) "
                    'ORDER BY created DESC LIMIT 1', (fingerprint, now)).fetchone()
                if existing:
                    return existing['id'], False
            db.execute('INSERT INTO operations(id,session,op,args,state,created,owner_pid,expires,fingerprint) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',
                       (job_id, session, op, encode(args), 'queued', now, os.getpid(), now + queue_timeout, fingerprint))
        return (job_id, True) if reuse else job_id

    def _archive(self, db, original, retained):
        old, new = canonical(original), canonical(retained)
        affected = db.execute('SELECT DISTINCT operation_id FROM edges WHERE output=? OR input=?',
                              (old, old)).fetchall()
        db.execute('UPDATE edges SET output=? WHERE output=?', (new, old))
        db.execute('UPDATE edges SET input=? WHERE input=?', (new, old))
        db.execute('INSERT OR REPLACE INTO datasets VALUES(?,?)', (new, str(retained)))
        def relocate(value):
            if isinstance(value, str) and os.path.isabs(value) and canonical(value) == old:
                return str(retained)
            if isinstance(value, list):
                return [relocate(v) for v in value]
            if isinstance(value, dict):
                return {k: v if k in ('params', 'parameters', 'requested_output', 'original_path',
                                     'source', 'in_features')
                        else relocate(v) for k, v in value.items()}
            return value
        for entry in affected:
            row = db.execute('SELECT result FROM operations WHERE id=?', (entry[0],)).fetchone()
            if row and row['result']:
                db.execute('UPDATE operations SET result=? WHERE id=?',
                           (encode(relocate(json.loads(row['result']))), entry[0]))

    def update(self, job_id, state, result=None, error=None, inputs=(), outputs=()):
        now = time.time()
        with self.connect() as db:
            for archive in (result or {}).get('archived_outputs', []):
                self._archive(db, archive['original_path'], archive['history_path'])
            if state in ('starting', 'running'):
                db.execute('UPDATE operations SET state=?,started=?,worker_pid=? WHERE id=?',
                           (state, now, os.getpid(), job_id))
            else:
                db.execute('UPDATE operations SET state=?,finished=?,result=?,error=? WHERE id=?',
                           (state, now, encode(result) if result is not None else None, error, job_id))
            if state == 'succeeded':
                for path in list(inputs) + list(outputs):
                    db.execute('INSERT OR IGNORE INTO datasets VALUES(?,?)', (canonical(path), str(path)))
                for output in outputs:
                    for source in list(inputs) or ['']:
                        db.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?)',
                                   (job_id, canonical(output), canonical(source) if source else ''))

    def event(self, job_id, stage, details):
        """Persist before executing the next stage; audit failure stops the operation."""
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM operations WHERE id=?', (job_id,)).fetchone():
                raise ValueError('当前项目中找不到 job_id：' + job_id)
            db.execute('INSERT INTO job_events(job_id,ts,stage,details) VALUES(?,?,?,?)',
                       (job_id, time.time(), stage, encode(details)))

    def events(self, job_id, after=0, limit=20):
        self.get(job_id)
        limit = max(1, min(100, int(limit)))
        with self.connect() as db:
            rows = db.execute('SELECT sequence,ts,stage,details FROM job_events '
                              'WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                              (job_id, max(0, int(after)), limit + 1)).fetchall()
        events = [{**dict(row), 'details': json.loads(row['details'])} for row in rows[:limit]]
        return {'job_id': job_id, 'events': events, 'has_more': len(rows) > limit,
                'next_after': events[-1]['sequence'] if events else after}

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('当前项目中找不到 job_id：' + job_id)
        data = dict(row)
        if data['state'] == 'queued' and data['expires'] and time.time() >= data['expires']:
            with self.connect() as db:
                db.execute("UPDATE operations SET state='failed',error=?,finished=? WHERE id=? AND state='queued'",
                           ('WorkerBusy：排队等待已超时，操作未执行。', time.time(), job_id))
            return self.get(job_id)
        pid = data['worker_pid'] if data['state'] == 'running' else data['owner_pid']
        if data['state'] in ('queued', 'starting', 'running') and not process_alive(pid):
            # Never overwrite a completion racing with this read.
            with self.connect() as db:
                db.execute("UPDATE operations SET state='unknown',error=?,finished=? "
                           "WHERE id=? AND state=?",
                           ('所属进程已退出，结果未确认；请检查保留成果和事务日志。',
                            time.time(), job_id, data['state']))
            return self.get(job_id)
        for key in ('args', 'result'):
            if data[key] is not None:
                data[key] = json.loads(data[key])
        data['ok'] = data['state'] == 'succeeded'
        data['job_id'] = data.pop('id')
        if data['state'] in ('failed', 'unknown'):
            data['transactions'] = []
            for manifest in (self.root / '.arcgis-mcp' / 'transactions').glob('*.json'):
                try:
                    item = json.loads(manifest.read_text(encoding='utf-8'))
                    if item.get('job_id') == job_id:
                        data['transactions'].append({'manifest': str(manifest), **item})
                except (OSError, ValueError):
                    continue
        return data

    def display(self, path):
        if not path:
            return None
        with self.connect() as db:
            original = db.execute('SELECT path FROM datasets WHERE key=?', (canonical(path),)).fetchone()
        if original:
            path = original[0]
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def lineage(self, path, depth=30, downstream=False, detail=True, offset=0, limit=None):
        depth = max(0, min(int(depth), 100))
        frontier = [(canonical(path), 0, time.time())]
        seen, steps, roots = set(), {}, set()
        truncated = False
        with self.connect() as db:
            while frontier:
                current, level, cutoff = frontier.pop(0)
                if level >= depth:
                    truncated = True
                    continue
                producer = db.execute('SELECT DISTINCT o.id,o.finished FROM edges e JOIN operations o '
                    'ON o.id=e.operation_id WHERE e.output=? AND o.state=? AND o.finished<=? '
                    'ORDER BY o.finished DESC LIMIT 1', (current, 'succeeded', cutoff)).fetchone()
                identity = (current, producer['id'] if producer else None)
                if identity in seen:
                    continue
                seen.add(identity)
                if downstream:
                    since = producer['finished'] if producer else 0
                    next_producer = db.execute('SELECT MIN(o.finished) FROM edges e JOIN operations o '
                        'ON o.id=e.operation_id WHERE e.output=? AND o.state=? AND o.finished>?',
                        (current, 'succeeded', since)).fetchone()[0]
                    rows = db.execute('SELECT DISTINCT e.operation_id FROM edges e JOIN operations o '
                        'ON o.id=e.operation_id WHERE e.input=? AND o.state=? '
                        'AND COALESCE(o.started,o.created)>=? AND COALESCE(o.started,o.created)<?',
                        (current, 'succeeded', since, next_producer or float('inf'))).fetchall()
                else:
                    rows = [{'operation_id': producer['id']}] if producer else []
                if not rows:
                    roots.add(current)
                for row in rows:
                    job_id = row['operation_id']
                    edges = db.execute('SELECT output,input FROM edges WHERE operation_id=?',
                                       (job_id,)).fetchall()
                    inputs = sorted({r['input'] for r in edges if r['input']})
                    outputs = sorted({r['output'] for r in edges})
                    item = self.get(job_id)
                    item['inputs'] = [self.display(p) for p in inputs]
                    item['outputs'] = [self.display(p) for p in outputs]
                    item['version_id'] = job_id
                    input_time = item['started'] or item['created']
                    item['input_versions'] = {}
                    for source in inputs:
                        previous = db.execute('SELECT DISTINCT o.id FROM edges e JOIN operations o '
                            'ON o.id=e.operation_id WHERE e.output=? AND o.state=? AND o.finished<=? '
                            'ORDER BY o.finished DESC LIMIT 1', (source, 'succeeded', input_time)).fetchone()
                        item['input_versions'][self.display(source)] = previous['id'] if previous else None
                    steps[job_id] = item
                    frontier.extend((p, level + 1, item['finished'] if downstream else input_time)
                                    for p in (outputs if downstream else inputs))
        ordered = sorted(steps.values(), key=lambda item: item['started'] or item['created'])
        total = len(ordered)
        offset = max(0, int(offset))
        page_limit = total if limit is None else max(1, min(100, int(limit)))
        ordered = ordered[offset:offset + page_limit]
        if not detail:
            compact = []
            for item in ordered:
                payload = item.get('result') or {}
                args = item.get('args') or {}
                compact.append({**{key: item[key] for key in ('job_id', 'version_id', 'state',
                    'created', 'started', 'finished', 'inputs', 'outputs', 'input_versions')},
                    'operation': payload.get('operation_summary') or payload.get('tool') or
                                 args.get('operation') or item['op'],
                    'params': payload.get('params') or payload.get('parameters') or args.get('params') or {},
                    **({'reason': args['reason']} if args.get('reason') else {})})
                if args.get('parent_job_id'):
                    compact[-1]['parent_job_id'] = args['parent_job_id']
                    compact[-1]['script_sha256'] = args.get('script_sha256')
                    compact[-1]['mode'] = args.get('mode', 'final')
                    compact[-1]['workflow_id'] = args.get('workflow_id')
            ordered = compact
        next_offset = offset + len(ordered) if offset + len(ordered) < total else None
        return {'path': self.display(canonical(path)), 'steps': ordered,
                'roots': [self.display(p) for p in sorted(roots)], 'depth': depth,
                'direction': 'downstream' if downstream else 'upstream',
                'truncated': truncated,
                'total_steps': total, 'returned': len(ordered), 'offset': offset,
                'next_offset': next_offset, 'page_truncated': next_offset is not None,
                'note': '外部手工修改不在审计范围；未记录的来源作为链路起点。'}
