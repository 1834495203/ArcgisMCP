"""端到端验证「数据源跟随项目目录」。

对**同一个服务实例**，用不同方式指定不同的项目目录，确认：
  1. 不给项目时没有写死的数据源（project_root=null，sources 为空，不再有 demo）
  2. 请求头 X-Project-Root 指向项目 → 自动扫描该项目的数据，零配置按名字就能读
  3. 同一实例上两个会话给不同项目头，各自只看到自己项目的数据，互不干扰
  4. 没有请求头时可以用 use_project 切换（这条也覆盖中文路径）
  5. 请求头里的目录不存在时，报错要说明怎么修

先启动服务（不要带 --project）：
    scripts\\run-http.bat
再运行：
    .venv\\Scripts\\python.exe tests\\project_scope_check.py [http://127.0.0.1:8765/mcp]
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from arcgis_mcp.core.project import encode_header_path  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8765/mcp"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

passed = 0
failed = 0
temp_root: Path | None = None


def check(label: str, got, expected) -> None:
    global passed, failed
    if got == expected:
        passed += 1
        print(f"  {GREEN}ok{RESET}   {label}")
    else:
        failed += 1
        print(f"  {RED}FAIL{RESET} {label}\n       期望: {expected}\n       实际: {got}")


def make_project(name: str) -> Path:
    """把示例数据拷进一个临时「项目」。name 里可以带中文，用来验证非 ASCII 路径。"""
    global temp_root
    if temp_root is None:
        temp_root = Path(tempfile.mkdtemp(prefix="arcgis-mcp-proj-"))
    project = temp_root / name
    (project / "数据").mkdir(parents=True)
    ignore = shutil.ignore_patterns("*.lock", "*.sr.lock")
    shutil.copytree(ROOT / "data" / "Demo.gdb", project / "数据" / "City.gdb", ignore=ignore)
    shutil.copytree(ROOT / "data" / "shp", project / "矢量", ignore=ignore)
    (project / ".venv" / "junk").mkdir(parents=True)   # 噪音目录，应被跳过
    (project / "notes.md").write_text("hello", encoding="utf-8")
    return project


async def call(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "raw": text}


async def with_session(url: str, headers: dict, body):
    client = httpx2.AsyncClient(headers=headers)
    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await body(session)


async def main(url: str) -> int:
    ascii_project = make_project("project_alpha")          # 纯 ASCII 路径 → 走请求头
    cn_project = make_project("西部规划项目")                # 中文路径 → 走 use_project
    print(f"临时项目目录：{temp_root}")
    print(f"  ASCII 项目：{ascii_project}")
    print(f"  中文  项目：{cn_project}\n")

    # ---------- 会话 1：不带任何项目信息 ----------
    print("会话 1：不带 X-Project-Root")

    async def body1(s):
        data = await call(s, "list_datasources", {})
        check("没有项目时不报错", data.get("ok"), True)
        check("project_root 为 null（没有写死的数据源）", data.get("project_root"), None)
        check("sources 为空（配置里已经没有 demo 了）", data.get("sources"), [])
        check("给出了怎么指定项目的指引", "how_to_set_project" in data, True)

        switched = await call(s, "use_project", {"folder": str(cn_project)})
        check("use_project 切到中文路径成功", switched.get("ok"), True)
        check("项目根 = 中文项目", switched.get("project_root"), str(cn_project))

        after = await call(s, "list_datasources", {})
        names = {w["alias"] for w in after.get("sources", [])}
        check("扫到 City.gdb（别名小写 city）", "city" in names, True)
        check("扫到含 shp 的「矢量」目录", "矢量" in names, True)
        check("噪音目录 .venv 被跳过", any("junk" in n for n in names), False)

        gdb = await call(s, "read_gdb_data", {"gdb": "City.gdb", "dataset": "cities", "limit": 2})
        check("中文路径下零配置读 gdb 成功", gdb.get("ok"), True)
        check("读的是中文项目里的文件", str(cn_project) in str(gdb.get("path")), True)

    await with_session(url, {}, body1)

    # ---------- 会话 2：请求头指向 ASCII 项目 ----------
    print("\n会话 2：X-Project-Root 指向 ASCII 项目")

    async def body2(s):
        data = await call(s, "list_datasources", {})
        check("project_root 来自请求头", data.get("project_root"), str(ascii_project))
        check("来源标记为 header", data.get("project_root_source"), "header")
        origins = {w["origin"] for w in data.get("sources", [])}
        check("数据源全部来自自动扫描", origins <= {"auto"}, True)

        gdb = await call(s, "read_gdb_data", {"gdb": "City.gdb", "dataset": "cities", "limit": 3})
        check("零配置读 gdb 成功", gdb.get("ok"), True)
        check("路径在 ASCII 项目里", str(ascii_project) in str(gdb.get("path")), True)
        check("返回 3 条", gdb.get("returned"), 3)

        shp = await call(s, "read_shp_data", {"shp": "roads", "limit": 2})
        check("零配置读 shp 成功", shp.get("ok"), True)
        check("shp 也在该项目里", str(ascii_project) in str(shp.get("path")), True)

        by_alias = await call(s, "list_gdb_datasets", {"gdb": "city"})
        check("自动生成的别名 city 可用", by_alias.get("ok"), True)
        check("列出了 gdb 里的全部数据集",
              sorted(i["relative_path"] for i in by_alias.get("items", []))
              if "items" in by_alias else None,
              ["base/rivers", "cities", "districts", "notes"])

    await with_session(url, {"X-Project-Root": str(ascii_project)}, body2)

    # ---------- 会话 3：百分号编码的中文路径也能通过请求头传 ----------
    print("\n会话 3：请求头用百分号编码传中文路径")

    async def body3(s):
        data = await call(s, "list_datasources", {})
        check("解回中文项目根", data.get("project_root"), str(cn_project))
        gdb = await call(s, "read_gdb_data", {"gdb": "city", "dataset": "cities", "limit": 1})
        check("编码过的路径也能正常读数据", gdb.get("ok"), True)

    encoded = encode_header_path(str(cn_project))
    print(f"  {DIM}header = X-Project-Root: {encoded}{RESET}")
    await with_session(url, {"X-Project-Root": encoded}, body3)

    # ---------- 会话 4：两个项目头并存，互不干扰 ----------
    print("\n会话 4：两个会话各给一个项目头（同一服务实例）")
    results: dict = {}

    async def body4a(s):
        data = await call(s, "list_datasources", {})
        gdb = await call(s, "read_gdb_data", {"gdb": "City.gdb", "dataset": "notes", "limit": 1})
        results["a"] = {"root": data.get("project_root"), "path": str(gdb.get("path"))}

    async def body4b(s):
        data = await call(s, "list_datasources", {})
        gdb = await call(s, "read_gdb_data", {"gdb": "demo", "dataset": "notes", "limit": 1})
        results["b"] = {"root": data.get("project_root"), "path": str(gdb.get("path"))}

    await asyncio.gather(
        with_session(url, {"X-Project-Root": str(ascii_project)}, body4a),
        with_session(url, {"X-Project-Root": str(ROOT)}, body4b),
    )
    check("会话 A 看到自己的项目", results["a"]["root"], str(ascii_project))
    check("会话 B 看到仓库项目", results["b"]["root"], str(ROOT))
    check("会话 A 读到的是自己项目的数据", str(ascii_project) in results["a"]["path"], True)
    check("会话 B 读到的是仓库的数据", str(ascii_project) not in results["b"]["path"], True)

    # ---------- 会话 5：请求头指向不存在的目录，要给出可操作的报错 ----------
    print("\n会话 5：请求头里的目录不存在")

    async def body5(s):
        data = await call(s, "list_datasources", {})
        problems = " ".join(data.get("problems", []))
        check("明确说明请求头里的目录不存在", "请求头里的项目目录不存在" in problems, True)
        check("提示了百分号编码的写法", "百分号编码" in problems, True)

    await with_session(url, {"X-Project-Root": str(temp_root / "does_not_exist")}, body5)

    print()
    if failed:
        print(f"{RED}共 {failed} 项失败，{passed} 项通过{RESET}")
    else:
        print(f"{GREEN}全部 {passed} 项通过 ✅{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    try:
        code = asyncio.run(main(target))
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
    raise SystemExit(code)
