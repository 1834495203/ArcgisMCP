"""验证 stdio 传输模式（给只支持 stdio 的 AI 客户端用）。

运行：
    .venv\\Scripts\\python.exe tests\\stdio_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


async def main() -> int:
    params = StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "arcgis_mcp", "--transport", "stdio"],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("工具：", ", ".join(t.name for t in tools.tools))
            result = await session.call_tool("read_gdb_data",
                                             {"gdb": "demo", "dataset": "cities", "limit": 2})
            text = "".join(getattr(c, "text", "") for c in result.content)
            data = json.loads(text)
            print("读取结果：ok =", data.get("ok"), "| returned =", data.get("returned"),
                  "| path =", data.get("path"))
            for record in data.get("records", []):
                print("  -", record)
            return 0 if data.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
