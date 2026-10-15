"""端到端验证最简用法：use_project 报一次项目目录 → 之后全用 ./ 相对路径。

这是最贴近日常的用法，也是用户最关心的路径：
    use_project(folder="D:\\projects\\myproject")
    read_gdb_data(gdb="./data/City.gdb", dataset="cities")
    read_shp_data(shp="./矢量/roads.shp")

先启动服务：
    scripts\\run-http.bat
再运行：
    .venv\\Scripts\\python.exe tests\\relative_path_check.py [http://127.0.0.1:8765/mcp]
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
DEFAULT_URL = "http://127.0.0.1:8765/mcp"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

passed = 0
failed = 0


def check(label: str, got, expected) -> None:
    global passed, failed
    if got == expected:
        passed += 1
        print(f"  {GREEN}ok{RESET}   {label}")
    else:
        failed += 1
        print(f"  {RED}FAIL{RESET} {label}\n       期望: {expected}\n       实际: {got}")


async def call(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "raw": text}


def build_project() -> Path:
    """临时项目，用中文目录名，确认 ./ 相对路径 + 中文都吃得下。"""
    base = Path(tempfile.mkdtemp(prefix="arcgis-mcp-rel-"))
    project = base / "myproject"
    (project / "data").mkdir(parents=True)
    ignore = shutil.ignore_patterns("*.lock", "*.sr.lock")
    shutil.copytree(ROOT / "data" / "Demo.gdb", project / "data" / "City.gdb", ignore=ignore)
    shutil.copytree(ROOT / "data" / "shp", project / "矢量", ignore=ignore)
    return project


async def main(url: str) -> int:
    project = build_project()
    print(f"临时项目：{project}\n")
    client = httpx2.AsyncClient()
    try:
        async with streamable_http_client(url, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # ---- 第 1 步：把项目根目录告诉服务端 ----
                print("第 1 步：use_project 报项目根目录")
                step1 = await call(session, "use_project", {"folder": str(project)})
                check("调用成功", step1.get("ok"), True)
                check("服务端记住了项目根目录", step1.get("project_root"), str(project))
                # 矢量 和 矢量/points 都含 shp，都会被扫出来
                check("顺手发现了项目里的数据",
                      sorted(w["alias"] for w in step1.get("sources", [])), ["city", "points", "矢量"])
                print(f"     {DIM}{step1.get('message')}{RESET}")

                # ---- 第 2 步：全部用 ./ 相对路径 ----
                print("\n第 2 步：用 ./ 相对路径读数据")

                g1 = await call(session, "read_gdb_data",
                                {"gdb": "./data/City.gdb", "dataset": "cities", "limit": 3})
                check("./data/City.gdb 读成功", g1.get("ok"), True)
                check("拼出来的是项目里的绝对路径", g1.get("path"), str(project / "data" / "City.gdb" / "cities"))
                check("返回 3 条", g1.get("returned"), 3)

                g2 = await call(session, "read_gdb_data",
                                {"gdb": "./data/City.gdb/cities", "limit": 1})
                check("./xxx.gdb/要素类 这种写法也认（dataset 可以不填）", g2.get("ok"), True)
                check("数据集被正确拆出来", g2.get("dataset"), "cities")

                g2b = await call(session, "read_gdb_data", {"gdb": "./data/City.gdb"})
                check("只给 gdb 不给 dataset 时，列出库里有什么", g2b.get("need_dataset"), True)
                check("列出的是全部数据集",
                      sorted(d["name"] for d in g2b.get("datasets", [])),
                      ["base/rivers", "cities", "districts", "notes"])

                g3 = await call(session, "read_gdb_data",
                                {"gdb": "./data/City.gdb", "dataset": "base/rivers", "limit": 0})
                check("要素数据集里的要素类（base/rivers）", g3.get("ok"), True)

                s1 = await call(session, "read_shp_data", {"shp": "./矢量/roads.shp", "limit": 2})
                check("./矢量/roads.shp 读成功", s1.get("ok"), True)
                check("shp 路径也拼对了", s1.get("path"), str(project / "矢量" / "roads.shp"))

                s2 = await call(session, "read_shp_data", {"shp": "./矢量/points/poi.shp", "limit": 5})
                check("./ 相对路径能找到子目录里的 shp", s2.get("ok"), True)

                d1 = await call(session, "list_gdb_datasets", {"gdb": "./data/City.gdb"})
                check("./ 相对路径列要素类", d1.get("ok"), True)
                check("列出的数据集齐全",
                      sorted(i["relative_path"] for i in d1.get("items", [])),
                      ["base/rivers", "cities", "districts", "notes"])

                # ---- 不写 ./ 也能用（按名字在项目里找） ----
                print("\n不写 ./ 时的按名字查找")
                n1 = await call(session, "read_gdb_data",
                                {"gdb": "City.gdb", "dataset": "cities", "limit": 1})
                check("只写库名也行", n1.get("ok"), True)
                n2 = await call(session, "read_shp_data", {"shp": "roads", "limit": 1})
                check("只写 shp 名也行", n2.get("ok"), True)

                # ---- 项目外/写错的路径要给可读的报错 ----
                print("\n路径写错时的报错")
                bad = await call(session, "read_gdb_data", {"gdb": "./nope/No.gdb", "dataset": "x"})
                check("报错里带上项目根目录，便于自查", str(project) in str(bad.get("error", "")), True)
    finally:
        shutil.rmtree(project.parent, ignore_errors=True)

    print()
    if failed:
        print(f"{RED}共 {failed} 项失败，{passed} 项通过{RESET}")
    else:
        print(f"{GREEN}全部 {passed} 项通过 ✅{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
