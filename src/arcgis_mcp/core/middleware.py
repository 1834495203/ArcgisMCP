"""中间件：统一记录每一次 MCP 请求（重点是工具调用）。

好处是工具代码里不用写任何「开始/结束/耗时」的样板，日志格式也统一：
    [arcgis-mcp] 12:00:01 INFO    call: read_gdb_data(gdb="./data/City.gdb", dataset="cities", limit=5)
    [arcgis-mcp] 12:00:01 INFO    call: read_gdb_data -> 完成 0.83s

它是一个「观察者」：只读 ctx.method / ctx.params，原样把结果透传，
不改写参数、不吞异常，所以对功能零影响。

另外它负责一件工具层做不到的事：**发现拼错的参数名**。
MCP 客户端/SDK 对多余的参数是静默忽略的（工具方法按名字取参数），
所以 AI 把 distance 拼成 distanse 时既不会报错、那个参数也不生效 —— 只能在这里看原始参数。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Set

from .logs import brief, get_logger

#: 参数摘要里最多显示多少字符
BRIEF_LIMIT = 160

#: 每个工具的合法参数名，注册工具时由 Tool.register() 填进来
_TOOL_PARAMETERS: Dict[str, Set[str]] = {}


def remember_tool_parameters(name: str, properties: Any) -> None:
    """由 Tool.register() 调用，记下该工具的参数名，供「拼错参数名」检查用。"""
    if isinstance(properties, dict) and properties:
        _TOOL_PARAMETERS[name] = set(properties)


class ToolCallLogger:
    """把每次 tools/call 的工具名、参数摘要、耗时和可疑参数写进日志。"""

    def __init__(self, log_arguments: bool = True) -> None:
        self.log = get_logger("call")
        self.log_arguments = log_arguments
        self._warned: Set[tuple] = set()

    async def __call__(self, ctx, call_next):
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)

        params = getattr(ctx, "params", None) or {}
        name: Optional[str] = None
        arguments: Any = None
        if isinstance(params, dict):
            name = params.get("name")
            arguments = params.get("arguments")

        suffix = f"({brief(arguments)})" if self.log_arguments and arguments else ""
        self.log.info("%s%s", name, suffix)
        if name and isinstance(arguments, dict):
            self._check_argument_names(name, arguments)

        started = time.perf_counter()
        try:
            result = await call_next(ctx)
        except Exception as exc:
            self.log.warning("%s !! 失败 %.2fs：%s: %s",
                             name, time.perf_counter() - started, type(exc).__name__, exc)
            raise
        self.log.info("%s -> 完成 %.2fs%s", name, time.perf_counter() - started,
                      self._size_note(result))
        return result

    def _check_argument_names(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        """参数名不在该工具 schema 里时告警（同一工具同一批参数只提醒一次）。"""
        known = _TOOL_PARAMETERS.get(tool_name)
        if not known:
            return
        unknown = [key for key in arguments if key not in known]
        if not unknown:
            return
        cache_key = (tool_name, tuple(sorted(unknown)))
        if cache_key in self._warned:
            return
        self._warned.add(cache_key)
        self.log.warning(
            "%s 收到了不认识的参数 %s —— 它会被静默忽略（调用本身照常执行，但这个参数没生效）。"
            "该工具可用参数：%s",
            tool_name, "、".join(unknown), "、".join(sorted(known)),
        )

    @staticmethod
    def _size_note(result: Any) -> str:
        """尽量报出返回体大小，方便发现「这次返回特别大」（对用户也直观）。"""
        # 结果可能被 ServerResult 包了一层（.root 才是 CallToolResult）
        for obj in (result, getattr(result, "root", None)):
            content = getattr(obj, "content", None)
            if not content:
                continue
            total = 0
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    total += len(text)
            if total:
                return f"，返回 {total} 字符"
        return ""
