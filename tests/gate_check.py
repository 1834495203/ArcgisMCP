"""验证「必须先调 use_project」这个硬约束是真的生效，而不只是写在说明里。

要点：
  1. 没指定项目根目录时，需读数据的 4 个工具必须全部**失败**（MCP is_error=true），
     且错误文本里给出 use_project 的调用方式；
  2. 纯发现类工具（list_datasources / arcgis_status / use_project）不受影响；
  3. 调用 use_project 之后，同样 4 个工具立刻可用；
  4. 配置里 require_project=false 时可以关掉这个约束（用 stdio + 环境变量验证，
     避免为了测试再起一个服务）。

先启动服务（不带 --project）：
    scripts\\run-http.bat
再运行：
    .venv\\Scripts\\python.exe tests\\gate_check.py [http://127.0.0.1:8765/mcp]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8765/mcp"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

passed = 0
failed = 0

# 需要项目根目录的工具，以及一个能触发路径解析的参数组合
GATED_TOOLS = [
    ("read_gdb_data", {"gdb": "./data/Demo.gdb", "dataset": "cities", "limit": 1}),
    ("read_shp_data", {"shp": "./data/shp/roads.shp", "limit": 1}),
    ("list_gdb_datasets", {"gdb": "./data/Demo.gdb"}),
    ("list_shp_datasets", {"folder": "./data/shp"}),
]

OPEN_TOOLS = [
    ("list_datasources", {}),
    ("arcgis_status", {}),
]


def check(label: str, got, expected) -> None:
    global passed, failed
    if got == expected:
        passed += 1
        print(f"  {GREEN}ok{RESET}   {label}")
    else:
        failed += 1
        print(f"  {RED}FAIL{RESET} {label}\n       期望: {expected}\n       实际: {got}")


async def raw_call(session: ClientSession, name: str, args: dict):
    """返回 (is_error, 文本)。"""
    result = await session.call_tool(name, args)
    raw = getattr(result, "is_error", None)
    if raw is None:
        raw = getattr(result, "isError", False)
    text = "".join(getattr(c, "text", "") for c in result.content)
    return bool(raw), text


async def main(url: str) -> int:
    client = httpx2.AsyncClient()   # 故意不带 X-Project-Root
    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("没指定项目根目录时：")
            for name, args in GATED_TOOLS:
                is_error, text = await raw_call(session, name, args)
                check(f"{name} 被拒绝（is_error=true）", is_error, True)
                check(f"{name} 的报错要求先调 use_project", "use_project" in text, True)
                check(f"{name} 的报错给出了示例写法", "use_project(folder=" in text, True)
            print(f"     {DIM}示例报错：{GATED_TOOLS[0][0]} -> "
                  f"{json.dumps((await raw_call(session, *GATED_TOOLS[0]))[1][:120], ensure_ascii=False)}{RESET}")

            print("\n发现类工具不受影响：")
            for name, args in OPEN_TOOLS:
                is_error, text = await raw_call(session, name, args)
                check(f"{name} 仍可用", is_error, False)
            is_error, text = await raw_call(session, "list_datasources", {})
            payload = json.loads(text)
            check("list_datasources 明确说 project_root 是 null", payload.get("project_root"), None)
            check("并给出怎么指定项目", "how_to_set_project" in payload, True)

            print("\n工具说明里的前置条件是基类自动拼的：")
            listed = {t.name: (t.description or "") for t in (await session.list_tools()).tools}
            gated_desc = listed["read_gdb_data"]
            check("需要项目的工具：说明以【前置条件】开头",
                  gated_desc.startswith("【前置条件】"), True)
            check("前置条件只出现一次（没被工具重复写一遍）",
                  gated_desc.count("【前置条件】"), 1)
            check("说明里同时保留了工具自己的文档",
                  "读取地理数据库" in gated_desc, True)
            ungated = listed["list_datasources"]
            check("不需要项目的工具：说明里没有前置条件",
                  "【前置条件】" not in ungated, True)
            check("gated / ungated 工具的说明都非空",
                  all(listed[n] for n in ("read_shp_data", "list_gdb_datasets", "list_shp_datasets",
                                          "use_project", "arcgis_status")), True)

            print("\n调用 use_project 之后：")
            is_error, text = await raw_call(session, "use_project", {"folder": str(ROOT)})
            check("use_project 成功", is_error, False)
            check("返回project_root", json.loads(text).get("project_root"), str(ROOT))

            for name, args in GATED_TOOLS:
                is_error, text = await raw_call(session, name, args)
                check(f"{name} 现在可以执行", is_error, False)
                check(f"{name} 返回正常结果", json.loads(text).get("ok"), True)

            print("\nuse_project 传错路径时：")
            is_error, text = await raw_call(session, "use_project",
                                            {"folder": str(ROOT / "does_not_exist")})
            check("报错而不是静默接受", is_error, True)
            check("提示要传绝对路径", "绝对路径" in text, True)

    # ---- require_project=false 时约束可关：直接验证门禁函数本身 ----
    print()
    print("require_project 开关（直接验证门禁函数）：")
    sys.path.insert(0, str(ROOT / "src"))
    from arcgis_mcp.core.config import load_settings
    from arcgis_mcp.core.project import ProjectView
    from arcgis_mcp.core.tool import require_project
    from arcgis_mcp.core.tool import ToolError

    base = load_settings()
    no_root = ProjectView(root=None, source="none", settings=base)

    base_required = load_settings()
    base_required.require_project = True
    view_required = ProjectView(root=None, source="none", settings=base_required)
    try:
        require_project(view_required, "read_gdb_data")
        raised = None
    except ToolError as exc:
        raised = str(exc)
    check("require_project=true（默认）：抛硬错误", raised is not None, True)
    check("错误里点名了具体工具", "read_gdb_data" in (raised or ""), True)
    check("错误里给了 use_project 的调用示例", "use_project(folder=" in (raised or ""), True)

    base_off = load_settings()
    base_off.require_project = False
    view_off = ProjectView(root=None, source="none", settings=base_off)
    try:
        require_project(view_off, "read_gdb_data")
        ok = True
    except ToolError:
        ok = False
    check("require_project=false：放行", ok, True)

    with_root = ProjectView(root=ROOT, source="header", settings=base_required)
    try:
        require_project(with_root, "read_gdb_data")
        ok2 = True
    except ToolError:
        ok2 = False
    check("已经有项目根目录时：直接放行", ok2, True)

    print()
    if failed:
        print(f"{RED}共 {failed} 项失败，{passed} 项通过{RESET}")
    else:
        print(f"{GREEN}全部 {passed} 项通过 ✅{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
