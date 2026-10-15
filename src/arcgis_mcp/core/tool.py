"""工具基类。

写一个新工具只需要三步：

    class MyTool(Tool):
        name = "my_tool"                     # = MCP 工具名 = 下面那个方法名

        def my_tool(self, ctx: Context, foo: str, bar: int = 0) -> str:
            \"\"\"工具说明（会原样作为工具描述发给 AI，写清楚参数和返回值）。\"\"\"
            view = self.view(ctx)             # 当前项目作用域
            self.require_project(view)        # 需要项目根目录时加这行（硬约束）
            data = self.arcpy("read", workspace=..., ...)   # 调 arcpy，自动记日志
            return self.dumps({"ok": True, ...})

然后把类加进 tools/__init__.py 的 ALL_TOOLS 即可。

方法签名里的类型注解就是 MCP 给 AI 的工具入参 schema，所以**别用 **kwargs**。
ctx 参数由 SDK 注入（不在 schema 里），放在第一位；其余参数按需给默认值。

基类提供了这些能力（子类直接用，不用重复写）：
- self.log / self.trace()      日志：self.log 写服务端 stderr；self.trace() 同时推给客户端
- self.view(ctx)               解析「当前项目根目录」并拿到合并后的设置
- self.require_project(view)   没项目根目录时抛 ToolError（硬约束，模型会停下来纠正）
- self.arcpy(op, **args)       调 arcpy 工作进程，带日志、异常包装和超时
- self.resolve_limit()         按配置收敛 limit
- self.error()                 数据级失败：返回 ok=false 的 JSON（带排查提示）
- self.dumps() / self.shrink()  JSON 序列化 / 响应体过大时自动削减
"""

from __future__ import annotations

import contextlib
import json
import time
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

from .config import Settings
from .logs import brief, get_logger
from .mcp_compat import Context, ToolError, request_headers
from .project import PROJECT_HEADER, ProjectRegistry, ProjectView
from .worker import ArcPyWorker

#: 「当前项目根目录」的 HTTP 请求头名（客户端可用来免掉 use_project）
PROJECT_ROOT_HEADER = PROJECT_HEADER

#: 需要项目根目录的工具，工具说明里统一加上这段。
#: 由基类按 needs_project 自动拼接 —— 工具里不要再手写一遍。
PROJECT_PRECONDITION = (
    "【前置条件】需要先确定项目根目录：先调用 use_project(folder=\"<项目根目录的绝对路径>\")"
    "（若客户端已通过 stdio 工作目录 / 请求头 X-Project-Root 告诉过服务端，则可以跳过）。"
)


@dataclass
class Services:
    """工具运行需要的共享组件，由 server.py 组装好后注入每个工具。"""

    settings: Settings
    projects: ProjectRegistry
    worker: ArcPyWorker
    mode: str = "http"  # http | stdio
    extra: Dict[str, Any] = field(default_factory=dict)


def require_project(view: ProjectView, tool: str) -> None:
    """硬约束：没有确定项目根目录时拒绝执行。

    故意抛 ToolError（而不是像数据级错误那样返回 ok=false 的 JSON）：
    抛出去客户端收到 is_error=True，模型会把它当成失败停下来纠正，
    这正是「必须先调 use_project」想要的强制效果。
    """
    if view.root is not None or not view.settings.require_project:
        return
    known = [w.alias for w in view.sources]
    detail = f"目前可用的数据源：{'、'.join(known)}。" if known else "目前没有任何可用的数据源。"
    raise ToolError(
        f"还不能执行 {tool}：服务端不知道你在哪个项目目录里，因此无法解析数据路径。\n"
        "\n"
        "请先调用一次：\n"
        '    use_project(folder="<项目根目录的绝对路径>")\n'
        "（项目根目录就是你正在工作的那个目录，在终端里 pwd 一下就能拿到，"
        "例如 D:\\\\projects\\\\myproject）\n"
        "\n"
        "调用成功后，本工具的参数就可以直接写 ./ 相对路径，例如 "
        'read_gdb_data(gdb="./data/City.gdb", dataset="cities")。\n'
        "这一步每个会话只需要做一次，服务端会记住。\n"
        f"\n{detail}"
    )


class Tool(ABC):
    """所有 MCP 工具的基类。子类必须定义 name，并实现同名方法。"""

    #: MCP 工具名，必须与子类里那个公开方法同名
    name: ClassVar[str] = ""
    #: 可选：给客户端 UI 看的短标题
    title: ClassVar[str] = ""
    #: 需要项目根目录就设 True —— 一处声明同时管两件事：
    #:   1) 工具说明里自动加上【前置条件】那段（不用手写）
    #:   2) self.view(ctx) 会顺带做门禁检查，没有项目根目录就抛 ToolError
    needs_project: ClassVar[bool] = False
    #: 可选：该工具自己的额外说明，会拼在说明最前面（通用提示放这里，别写进每个方法的 docstring）
    notice: ClassVar[str] = ""
    timeout: ClassVar[Optional[float]] = None

    def __init__(self, services: Services) -> None:
        if not self.name:
            raise TypeError(f"{type(self).__name__} 必须定义类属性 name")
        entry = getattr(self, self.name, None)
        if not callable(entry):
            raise TypeError(
                f"{type(self).__name__} 必须实现名为 {self.name!r} 的方法"
                "（约定：工具名 = 方法名，方法签名就是工具的入参 schema）"
            )
        self.services = services
        self.log = get_logger(f"tools.{self.name}")
        self._entrypoint = entry

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    @property
    def entrypoint(self):
        """交给 MCP 注册的入口（就是子类那个带类型注解的同名方法）。"""
        return self._entrypoint

    def build_description(self) -> str:
        """工具说明：前置条件（按 needs_project 自动加）+ 自定义 notice + 方法 docstring。"""
        parts: List[str] = []
        if self.needs_project:
            parts.append(PROJECT_PRECONDITION)
        if self.notice:
            parts.append(self.notice)
        if hasattr(self, 'operation'):
            parts.append('【版本保留】编辑先复制再操作，输入原件保留；delete_dataset 会归档并移除原路径。'
                         '后续步骤必须使用本次返回的 output / out_features 实际路径。'
                         '返回 pending=true 时先用 job_result 获取完成结果。'
                         'overwrite=true 会先归档旧目标再替换指定路径；旧数据保留。')
        doc = (self.entrypoint.__doc__ or "").strip()
        if doc:
            parts.append(doc)
        return "\n\n".join(parts)

    def register(self, server) -> None:
        tool = server.tool(name=self.name, description=self.build_description())(self.entrypoint)
        # 把参数名记给中间件：MCP 会静默忽略多余的参数，只有中间件能看到原始参数，
        # 由它来提醒「某个参数名拼错了/没生效」。
        with contextlib.suppress(Exception):
            from .middleware import remember_tool_parameters

            schema = getattr(tool, "input_schema", None) or getattr(tool, "parameters", None) or {}
            remember_tool_parameters(self.name, schema.get("properties"))
        self.log.debug("已注册工具 %s", self.name)

    # ------------------------------------------------------------------
    # 上下文 / 项目作用域
    # ------------------------------------------------------------------
    @staticmethod
    def header(ctx: Context, name: str) -> Optional[str]:
        """读请求头（大小写不敏感）。stdio 或没有请求对象时返回 None。"""
        return request_headers(ctx).get(name.lower())

    @staticmethod
    def session_key(ctx: Context) -> str:
        """同一个客户端会话用一个 key，用来记住 use_project 的选择。"""
        headers = request_headers(ctx)
        return headers.get("mcp-session-id") or "default-session"

    def view(self, ctx: Context) -> ProjectView:
        """解析当前项目作用域（请求头 / 会话 / 环境变量 / cwd）。

        工具类声明了 `needs_project = True` 时，这里会顺带做门禁检查：
        没有项目根目录直接抛 ToolError（模型会停下来先调 use_project）。
        所以带前置条件的工具里只需要 `view = self.view(ctx)` 一行。
        """
        view = self.services.projects.view(
            header=self.header(ctx, PROJECT_ROOT_HEADER),
            session_key=self.session_key(ctx),
        )
        if self.needs_project:
            require_project(view, self.name)
        return view

    def switch_project(self, ctx: Context, folder: str) -> ProjectView:
        """把当前会话的项目根目录切到 folder（绝对路径），失败抛 ToolError。"""
        try:
            return self.services.projects.set_session_project(self.session_key(ctx), folder)
        except NotADirectoryError as exc:
            raise ToolError(
                f"{exc}\n"
                "folder 要传项目根目录的绝对路径，例如 D:\\\\projects\\\\myproject；"
                "不存在的目录不能作为项目根目录。"
            ) from exc

    def require_project(self, view: ProjectView) -> None:
        require_project(view, self.name)

    def project_block(self, view: ProjectView) -> Dict[str, Any]:
        """每个返回值里都带上「当时认为的项目根目录」，出问题好排查。"""
        block: Dict[str, Any] = {
            "project_root": str(view.root) if view.root else None,
            "project_root_source": view.source,
        }
        if view.root is None:
            block["how_to_set_project"] = (
                "服务端没识别到当前项目目录。请任选一种方式指定："
                '1) 调用 use_project(folder="项目文件夹的完整路径")；'
                "2) 让客户端在配置里带上请求头 X-Project-Root 指向项目目录；"
                "3) 改用 stdio 模式，由客户端的工作目录自动识别。"
            )
        return block

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def trace(self, message: str, ctx: Context | None = None, level: str = "info") -> None:
        """记一条日志；给了 ctx 就同时推给 MCP 客户端（AI 能看到进度）。

        工具方法是同步的、跑在 SDK 的工作线程里，而 ctx.info() 是协程，
        所以用 anyio.from_thread.run 调回事件循环（实测可行）。
        客户端不支持、或本方法不在工作线程里被调用时，静默降级为只写 stderr——
        推日志失败绝不能影响工具本身的执行。
        """
        logger = getattr(self.log, level, None) or self.log.info
        logger(message)
        if ctx is None:
            return
        send = getattr(ctx, level, None)
        if not callable(send):
            return
        try:
            import anyio

            anyio.from_thread.run(send, f"[{self.name}] {message}")
        except Exception:
            self.log.debug("无法把日志推送给客户端（不影响本次调用）", exc_info=True)

    # ------------------------------------------------------------------
    # arcpy
    # ------------------------------------------------------------------
    def arcpy(self, op: str, ctx: Context | None = None, **args: Any) -> Dict[str, Any]:
        """调用 arcpy 工作进程，自动记录耗时与失败原因。"""
        started = time.perf_counter()
        self.log.debug("arcpy.%s 入参：%s", op, brief(args))
        try:
            settings = self.settings
            if op in ('geoprocess', 'edit', 'run_arcpy_tool'):
                if ctx is None:
                    raise ToolError('写操作必须带项目和会话上下文')
                view = self.view(ctx)
                require_project(view, self.name)
                if view.root is None:
                    raise ToolError('版本审计写操作必须指定项目根目录')
                settings = view.settings
                name = args.get('operation') or args.get('tool', '')
                base = str(name).split('.')[-1].rsplit('_', 1)[0].lower() if '_' in str(name) else str(name).split('.')[-1].lower()
                if base.startswith('pairwise'):
                    base = base[len('pairwise'):]
                budget = self.timeout or settings.tool_timeouts.get(name) or settings.tool_timeouts.get(base) or settings.call_timeout
                from .jobs import Jobs
                worker = self.services.worker
                # Protect initialization only; never hold the worker execution lock during a soft wait.
                with worker._jobs_lock:
                    if not hasattr(worker, 'jobs'):
                        worker.jobs = Jobs(worker)
                    jobs = worker.jobs
                result = jobs.submit(view.root, self.session_key(ctx), op, args, budget,
                                     min(settings.job_wait_seconds, budget, 5), settings.queue_timeout)
            elif op == 'run_arcpy_code':
                raise WorkerError('审计保留模式不允许任意 Python 代码绕过复制和锁；请使用固定工具或 run_arcpy_tool。')
            else:
                result = self.services.worker.call(op, timeout=self.timeout or settings.read_timeout,
                                                   queue_timeout=min(settings.queue_timeout, 5), **args)
        except Exception as exc:
            self.log.warning("arcpy.%s 失败（%.2fs）：%s", op, time.perf_counter() - started, exc)
            raise
        self.log.info("arcpy.%s 完成（%.2fs）%s", op, time.perf_counter() - started, brief(result))
        return result

    # ------------------------------------------------------------------
    # 返回值处理
    # ------------------------------------------------------------------
    @property
    def settings(self) -> Settings:
        return self.services.settings

    def resolve_limit(self, limit: Optional[int], view: Optional[ProjectView] = None) -> int:
        settings = view.settings if view is not None else self.settings
        if limit is None:
            return settings.default_limit
        return max(0, min(int(limit), settings.max_limit))

    @staticmethod
    def dumps(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    def error(self, kind: str, exc: BaseException, view: Optional[ProjectView] = None,
              **extra: Any) -> str:
        """数据级失败（比如名字找不到）：返回 ok=false 的 JSON，带上排查线索。

        与 require_project 的区别：这种失败是「可以自我纠正」的，
        所以把候选、提示一并返回，让 AI 直接据此重试。
        """
        self.log.info("返回业务错误（%s）：%s", kind, str(exc).splitlines()[0] if str(exc) else exc)
        payload: Dict[str, Any] = {"ok": False, "error_type": kind, "error": str(exc), **extra}
        if view is not None:
            payload.update(self.project_block(view))
        return self.dumps(payload)

    def shrink(self, payload: Dict[str, Any], view: Optional[ProjectView] = None) -> Dict[str, Any]:
        """响应体超过配置上限时按需削减记录数，并说明原因。"""
        settings = view.settings if view is not None else self.settings
        max_bytes = settings.max_response_bytes
        if len(self.dumps(payload).encode("utf-8")) <= max_bytes:
            return payload
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            return payload
        kept = records
        while kept:
            kept = kept[: max(len(kept) // 2, 1)]
            payload = dict(payload)
            payload["records"] = kept
            payload["returned"] = len(kept)
            payload["truncated"] = True
            payload["truncated_reason"] = (
                f"响应体超过 {max_bytes} 字节上限，只保留了前 {len(kept)} 条记录；"
                "请用更小的 limit + offset 分页读取，或用 fields 只取需要的字段。"
            )
            if len(self.dumps(payload).encode("utf-8")) <= max_bytes:
                return payload
        return payload

    def pagination_hint(self, payload: Dict[str, Any], offset: int, result: Dict[str, Any]) -> Dict[str, Any]:
        """还有数据没返回时，补上 next_offset 和翻页提示。"""
        if result.get("truncated"):
            payload["next_offset"] = int(offset or 0) + result.get("returned", 0)
            payload["hint"] = (
                f"还有未返回的数据：用 offset={payload['next_offset']} 继续翻页，"
                "或用 where / fields 缩小范围。"
            )
        return payload


__all__ = ["Tool", "Services", "require_project", "PROJECT_ROOT_HEADER", "Context", "ToolError"]
