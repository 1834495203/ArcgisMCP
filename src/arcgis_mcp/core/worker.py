"""管理 arcpy 工作进程：启动、保活、超时、崩溃重启。

MCP 服务端（Python 3.12）通过 stdin/stdout 与core/arcpy_worker.py（Python 3.9 + arcpy）
通信。arcpy 的 import 很慢（首次十几秒到几十秒），所以进程只启动一次并常驻复用。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import PROJECT_ROOT, Settings
from .history import DATA_POLICY_VERSION

SENTINEL = "##ARCGIS_MCP##"


class WorkerError(RuntimeError):
    """arcpy 侧返回的业务错误（消息已经写得很直白，可以直接给 AI 看）。"""


class _WorkerDead(RuntimeError):
    """内部使用：子进程挂了，需要重启。"""


def _log(message: str) -> None:
    print(f"[arcgis-mcp] {message}", file=sys.stderr, flush=True)


class ArcPyWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._seq = 0
        self._responses: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._readers: list[threading.Thread] = []
        self._started_at: Optional[float] = None
        self._last_ping: Optional[Dict[str, Any]] = None
        self._ready = False  # 当前这个进程是否已经成功跑过一次（首次要算上 import arcpy 的时间）
        self._current_op = None
        self._operation_started = None
        self._waiting = 0
        self._jobs_lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            'data_policy_version': DATA_POLICY_VERSION,
            'ready': self._ready,
            "pid": self._proc.pid if self._proc else None,
            "python": self.settings.arcpy_python,
            "uptime_seconds": round(time.time() - self._started_at, 1) if self._started_at else None,
            "last_ping": self._last_ping,
            "current_op": self._current_op,
            "elapsed_seconds": round(time.monotonic() - self._operation_started, 1)
                if self._operation_started is not None else None,
            "queued_calls": self._waiting,
            'queued_jobs': self.jobs.queue.qsize() if hasattr(self, 'jobs') else 0,
        }

    # ------------------------------------------------------------------
    def _spawn_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        src = str(PROJECT_ROOT / "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join([p for p in (src, existing) if p])
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["ARCGIS_MCP_WORKER"] = "1"
        env.pop("PYTHONHOME", None)  # 避免污染 ArcGIS 自带 Python
        return env

    def ensure_started(self) -> None:
        with self._lock:
            if not self.running:
                self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return  # 已经活着就别再起一个（否则旧进程会被泄漏，还各自重复 import arcpy）
        exe = self.settings.arcpy_python_path
        if not exe.exists():
            raise WorkerError(
                f"找不到 arcpy 的 Python 解释器：{exe}\n"
                "请确认 ArcGIS Pro 已安装，并在 config/datasources.json 里修正 arcpy_python。"
            )
        _log(f"启动 arcpy 工作进程：{exe}（首次启动需要 import arcpy，可能要 10-40 秒）")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            # 按文件路径直接运行，**不要**用 -m arcgis_mcp.core.arcpy_worker：
            # -m 会先 import 包，而 core/__init__.py 会连带 import mcp SDK，
            # 但 arcpy 那个 Python 3.9 里没有 mcp，worker 会一启动就崩。
            [str(exe), "-u", str(Path(__file__).with_name("arcpy_worker.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            env=self._spawn_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._started_at = time.time()
        self._ready = False
        for target, stream, name in (
            (self._read_stdout, self._proc.stdout, "stdout"),
            (self._read_stderr, self._proc.stderr, "stderr"),
        ):
            thread = threading.Thread(target=target, args=(stream,), name=f"arcpy-{name}", daemon=True)
            thread.start()
            self._readers.append(thread)

    def _read_stdout(self, stream) -> None:
        for line in stream:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if line.startswith(SENTINEL):
                try:
                    self._responses.put(json.loads(line[len(SENTINEL):]))
                except json.JSONDecodeError:
                    _log(f"无法解析 worker 响应：{line[:500]}")
            else:
                _log(f"arcpy: {line}")

    def _read_stderr(self, stream) -> None:
        for line in stream:
            line = line.rstrip("\r\n")
            if line:
                _log(f"arcpy: {line}")

    # ------------------------------------------------------------------
    def call(self, op: str, timeout: Optional[float] = None,
             queue_timeout: float = 60, **args: Any) -> Dict[str, Any]:
        """调用一次 arcpy 操作。串行执行（arcpy 本身也不是线程安全的）。

        工作进程只启动一次并复用：arcpy 的 import 很贵，绝不能每次调用都重启。
        """
        self._waiting += 1
        acquired = self._lock.acquire(timeout=max(0, queue_timeout))
        self._waiting -= 1
        if not acquired:
            raise WorkerError('WorkerBusy：排队等待超时；操作尚未发送，请稍后重试。')
        try:
            if args.get('_audit'):
                from .history import History
                audit = args['_audit']
                history = History(audit['root'])
                if history.get(audit['job_id'])['state'] == 'failed':
                    raise WorkerError('WorkerBusy：排队已超时，操作未发送。')
                history.update(audit['job_id'], 'starting')
            last_error: Optional[BaseException] = None
            for attempt in (1, 2):
                if attempt == 2:
                    _log("worker 已退出，自动重启后重试一次")
                    self._stop_locked()
                if not self.running:
                    self._start_locked()
                # 刚起来的进程还没 import arcpy，给它宽松的超时；
                # 已经跑过一次的进程就用正常的调用超时。
                budget = timeout if timeout is not None else self.settings.call_timeout
                if not self._ready and op == 'ping':
                    budget = self.settings.startup_timeout
                if budget <= 0:
                    raise WorkerError('timeout 必须大于 0')
                try:
                    if not self._ready and op != 'ping':
                        self._call_locked('ping', {}, self.settings.startup_timeout)
                        self._ready = True
                    result = self._call_locked(op, args, budget)
                except _WorkerDead as exc:
                    if op not in ('ping', 'read', 'describe', 'list_datasets',
                                  'list_operations', 'list_arcpy_tools', 'describe_arcpy_tool'):
                        self._stop_locked()
                        raise WorkerError('WorkerLost：写操作执行结果未知，禁止自动重试。'
                                          '请查询 job_status 和保留的暂存成果。') from exc
                    last_error = exc
                    continue
                self._ready = True
                return result
            raise WorkerError(
                f"arcpy 工作进程异常退出，操作 {op!r} 失败：{last_error}\n"
                "请检查 ArcGIS Pro 授权状态，或在日志（stderr）里看 arcpy 的报错。"
            )
        finally:
            self._current_op = None
            self._operation_started = None
            self._lock.release()

    def _call_locked(self, op: str, args: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        assert self._proc is not None and self._proc.stdin is not None
        self._seq += 1
        self._current_op = args.get('operation') or args.get('tool') or op
        self._operation_started = time.monotonic()
        req_id = self._seq
        payload = json.dumps({"id": req_id, "op": op, "args": args}, ensure_ascii=False)
        try:
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _WorkerDead(f"写入 worker 失败：{exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop_locked()
                raise WorkerError(
                    f"arcpy 达到硬执行上限（{timeout:.0f} 秒），已终止 worker：{op}\n"
                    "结果可能未知；输入原件保留，请查询 job_status。不要盲目重试写操作。"
                )
            try:
                message = self._responses.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise _WorkerDead(f"worker 退出码 {self._proc.returncode}")
                continue

            if message.get("id") != req_id:
                _log(f"丢弃过期响应 id={message.get('id')}")
                continue
            if message.get("ok"):
                result = message.get("result") or {}
                if op == "ping":
                    self._last_ping = result
                return result
            error = message.get("error") or {}
            detail = error.get("message", "未知错误")
            if error.get("traceback"):
                detail = f"{detail}\n--- worker traceback ---\n{error['traceback']}"
            raise WorkerError(detail)

    # ------------------------------------------------------------------
    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._ready = False
        readers, self._readers = self._readers, []
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        # 等读取线程收尾，避免重启后旧进程的响应混进队列
        for thread in readers:
            thread.join(timeout=2)
        # 清掉残留响应，避免下次调用读到旧消息
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break

    def shutdown(self) -> None:
        with self._jobs_lock:
            if hasattr(self, 'jobs'):
                self.jobs.close()
        with self._lock:
            self._stop_locked()
