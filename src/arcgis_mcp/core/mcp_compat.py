"""mcp SDK 的版本兼容导入。

mcp 1.x 用 FastMCP，2.x 改名为 MCPServer（旧名会直接报错）；ToolError 的位置也不同。
集中在这里，别的地方只管 `from .mcp_compat import Context, MCPServer, ToolError`。

注意：本模块是**唯一**允许出现 try/except ImportError 的地方。
"""

from __future__ import annotations

try:  # pragma: no cover - 取决于安装的 mcp 版本
    from mcp.server.fastmcp import Context as Context  # type: ignore
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore
except Exception:  # pragma: no cover
    from mcp.server.mcpserver import Context as Context  # type: ignore
    from mcp.server.mcpserver import MCPServer as MCPServer  # type: ignore

try:  # pragma: no cover
    from mcp.server.mcpserver.exceptions import ToolError as ToolError  # type: ignore
except Exception:  # pragma: no cover
    try:
        from mcp.server.fastmcp.exceptions import ToolError as ToolError  # type: ignore
    except Exception:

        class ToolError(Exception):  # type: ignore[no-redef]
            """兜底：老 SDK 没有 ToolError，抛普通异常同样能把消息带给模型。"""


def request_headers(ctx) -> dict:
    """拿到请求头（stdio 或没有请求对象时返回空 dict）。"""
    headers = getattr(ctx, "headers", None) if ctx is not None else None
    if not headers:
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in headers.items()}
    except AttributeError:
        return {}
