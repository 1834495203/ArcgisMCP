"""端到端冒烟测试：用官方 MCP 客户端连真实 HTTP 服务，逐个调用工具并校验结果。

先启动服务：
    scripts\\run-http.bat                     （或 python -m arcgis_mcp）
再运行：
    .venv\\Scripts\\python.exe tests\\smoke_client.py [http://127.0.0.1:8765/mcp]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8765/mcp"

# 服务端的项目作用域是跟着客户端走的：这里把「本仓库」作为项目目录告诉服务端，
# 于是仓库里 .arcgis-mcp/datasources.json 定义的 demo / shp_data 别名才可用。
PROJECT_HEADER = {"X-Project-Root": str(ROOT)}

# (工具名, 参数, 断言说明)
CASES: List[Tuple[str, Dict[str, Any], str]] = [
    ("list_datasources", {}, "能列出别名"),
    ("arcgis_status", {}, "arcpy 就绪"),
    ("list_gdb_datasets", {"gdb": "demo"}, "gdb 里能列出 cities/rivers/notes"),
    ("list_shp_datasets", {"folder": "shp_data", "recursive": True}, "能找到 roads 和 poi"),
    ("read_gdb_data", {"gdb": "demo", "dataset": "cities", "limit": 3}, "读要素类"),
    ("read_gdb_data", {"gdb": "demo", "dataset": "base/rivers", "limit": 0}, "要素数据集里的要素类 + 只看 schema"),
    ("read_gdb_data", {"gdb": "demo", "dataset": "cities", "where": "POP > 20000000",
                       "fields": ["NAME", "POP"], "limit": 10}, "where + fields"),
    ("read_gdb_data", {"gdb": "Demo.gdb", "dataset": "notes", "limit": 5}, "只给名字不给路径"),
    ("read_gdb_data", {"gdb": "demo/cities", "dataset": "cities", "limit": 1}, "别名/要素类 写法"),
    ("read_shp_data", {"shp": "roads", "folder": "shp_data", "limit": 2, "geometry": "wkt"}, "读 shp + WKT"),
    ("read_shp_data", {"shp": "poi", "limit": 5, "geometry": "xy"}, "不给 folder，按名字搜到子目录里的 shp"),
    ("read_gdb_data", {"gdb": "demo", "dataset": "does_not_exist"}, "错误信息应带候选"),
    ("read_gdb_data", {"gdb": "no_such_source", "dataset": "x"}, "错误信息应带别名清单"),
]

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def summarize(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return f"<非 JSON 响应> {text[:200]}"
    if data.get("ok") is False:
        return f"ok=false :: {str(data.get('error'))[:160]}"
    parts = []
    if "records" in data:
        parts.append(f"returned={data.get('returned')}/{data.get('matched_count')}")
    if "items" in data:
        parts.append(f"items={data.get('count', len(data['items']))}")
    if "fields" in data:
        parts.append("fields=" + ",".join(f["name"] for f in data["fields"]))
    if data.get("shape_type"):
        parts.append(f"shape={data['shape_type']}")
    if "resolved_path" in data:
        parts.append(f"path={data['resolved_path']}")
    return " | ".join(parts) if parts else json.dumps(data, ensure_ascii=False)[:160]


async def main(url: str) -> int:
    failures = 0
    client = httpx2.AsyncClient(headers=PROJECT_HEADER)
    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"已连接 {url}")
            print(f"服务端工具（{len(names)}）：{', '.join(names)}\n")
            for expected in ("list_datasources", "read_gdb_data", "read_shp_data",
                             "list_gdb_datasets", "list_shp_datasets", "arcgis_status"):
                if expected not in names:
                    print(f"{RED}[缺失工具]{RESET} {expected}")
                    failures += 1

            for name, args, note in CASES:
                try:
                    result = await session.call_tool(name, args)
                except Exception as exc:  # noqa: BLE001
                    print(f"{RED}FAIL{RESET} {name}({json.dumps(args, ensure_ascii=False)}) "
                          f"-> 调用异常 {exc}")
                    failures += 1
                    continue
                text = "".join(getattr(c, "text", "") for c in result.content)
                payload: Dict[str, Any] = {}
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    pass

                expect_error = "错误信息" in note or "no_such_source" in str(args)
                # mcp 1.x 用 isError，2.x 用 is_error
                raw_error = getattr(result, "is_error", None)
                if raw_error is None:
                    raw_error = getattr(result, "isError", False)
                is_error = payload.get("ok") is False or bool(raw_error)
                if expect_error:
                    status = "PASS" if is_error and not payload.get("ok", True) else "FAIL"
                else:
                    status = "PASS" if payload.get("ok") and not is_error else "FAIL"
                if status == "FAIL":
                    failures += 1
                color = GREEN if status == "PASS" else RED
                print(f"{color}{status}{RESET} {name}({json.dumps(args, ensure_ascii=False)})"
                      f"  {DIM}// {note}{RESET}")
                print(f"     {summarize(text)[:300]}")

    print()
    if failures:
        print(f"{RED}共 {failures} 项未通过{RESET}")
    else:
        print(f"{GREEN}全部 {len(CASES)} 项通过 ✅{RESET}")
    return 1 if failures else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
