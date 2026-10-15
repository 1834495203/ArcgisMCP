"""One FIFO dispatcher per worker; soft wait never cancels or loses the result."""
from __future__ import annotations

import queue
import threading
import time

from .history import History, DATA_POLICY_VERSION
from .worker import WorkerError


class Jobs:
    def __init__(self, worker):
        self.worker = worker
        self.queue = queue.Queue(maxsize=100)
        self._state_lock = threading.Lock()
        self._closed = False
        self.thread = threading.Thread(target=self._run, name='arcgis-jobs', daemon=True)
        self.thread.start()

    def submit(self, root, session, op, args, budget, wait_seconds, queue_timeout, policy=None):
        history = History(root)
        job_id, created = history.create(op, args, session, queue_timeout, reuse=True)
        done = threading.Event()
        task = (history, job_id, op, args, budget, queue_timeout, time.monotonic(), done, policy or {})
        try:
            with self._state_lock:
                if self._closed:
                    if created:
                        history.update(job_id, 'failed', error='服务正在关闭，操作未执行。')
                    raise WorkerError('服务正在关闭，操作未执行。')
                if created:
                    self.queue.put_nowait(task)
        except queue.Full:
            history.update(job_id, 'failed', error='WorkerBusy：任务队列已满，操作未执行。')
            raise WorkerError('WorkerBusy：任务队列已满，操作未执行。')
        done.wait(max(0, min(wait_seconds, queue_timeout)))
        snapshot = history.get(job_id)
        state = snapshot['state']
        if state == 'succeeded':
            return {**snapshot['result'], 'job_id': job_id, 'job_state': state, 'deduplicated': not created}
        if state in ('failed', 'unknown'):
            return {'ok': False, 'job_id': job_id, 'job_state': state, 'error': snapshot['error']}
        return {'ok': True, 'pending': True, 'job_id': job_id, 'job_state': state,
                'data_policy_version': DATA_POLICY_VERSION,
                'deduplicated': not created,
                'note': '任务仍在排队或后台执行；请用 job_status / job_result 查询，勿重复提交。'}

    def _run(self):
        while True:
            task = self.queue.get()
            if task is None:
                self.queue.task_done()
                return
            history, job_id, op, args, budget, queue_timeout, queued, done, policy = task
            try:
                if history.get(job_id)['state'] == 'failed':
                    continue
                remaining = queue_timeout - (time.monotonic() - queued)
                if remaining <= 0:
                    history.update(job_id, 'failed', error='WorkerBusy：排队超时，操作未执行。')
                    continue
                result = self.worker.call(op, timeout=budget * 2, queue_timeout=remaining,
                    _audit={'root': str(history.root), 'job_id': job_id, **policy}, **args)
                # The worker commits history before emitting the response. Never overwrite it here.
                if history.get(job_id)['state'] != 'succeeded':
                    history.update(job_id, 'unknown', result=result,
                                   error='工具已返回，但审计提交未确认，请检查事务日志。')
            except Exception as exc:
                try:
                    snapshot = history.get(job_id)
                    if snapshot['state'] not in ('succeeded', 'failed'):
                        history.update(job_id, 'failed' if 'WorkerBusy' in str(exc) else 'unknown',
                                       error=str(exc))
                except Exception:
                    pass  # Existing durable queued/running entry remains discoverable.
            finally:
                done.set()
                self.queue.task_done()

    def close(self):
        """Finish dispatched work; do not start queued writes during shutdown."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    task = self.queue.get_nowait()
                except queue.Empty:
                    break
                history, job_id, _, _, _, _, _, done, _ = task
                try:
                    history.update(job_id, 'failed', error='服务关闭前尚未执行，任务已取消。')
                finally:
                    done.set()
                    self.queue.task_done()
            self.queue.put_nowait(None)
        self.thread.join()  # Running task retains its normal hard deadline.
