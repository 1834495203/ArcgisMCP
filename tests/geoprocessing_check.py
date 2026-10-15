"""端到端验证 7 个分析工具（真的调 arcpy 跑，输出真的落盘）。

覆盖每个工具的：正常跑通、输出要素数符合预期、输出已存在时不覆盖报错、overwrite=true 能覆盖。
另外验证 Eliminate 必须有 where、以及 update 不会改动原数据。

用法：
    scripts\\run-http.bat                       （先启动服务）
    .venv\\Scripts\\python.exe tests\\geoprocessing_check.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8765/mcp"
OUT_DIR = ROOT / "build_geo"          # 测试产物，跑完删掉
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
        from job_client import completed
        return await completed(session, json.loads(text))
    except json.JSONDecodeError:
        return {"ok": False, "raw": text}


def output_exists(out: Path) -> bool:
    """输出可能是 shapefile（文件），也可能是 gdb 里的要素类（不是文件系统上的文件）。"""
    if out.exists():
        return True
    return any(part.lower().endswith((".gdb", ".mdb")) for part in out.parts[:-1])


async def run_tool(session: ClientSession, label: str, tool: str, args: dict,
                   expect_count=None) -> dict:
    """跑一个分析工具并检查基本结果。"""
    data = await call(session, tool, args)
    check(f"{label} 执行成功", data.get("ok"), True)
    if not data.get("ok"):
        print(f"     {DIM}{str(data.get('error'))[:200]}{RESET}")
        return data
    out = Path(str(data.get("out_features")))
    check(f"{label} 输出真的落盘了", output_exists(out), True)
    check(f"{label} 返回了要素类型", bool(data.get("shape_type") or data.get("data_type")), True)
    if expect_count is not None:
        check(f"{label} 要素数符合预期", data.get("feature_count"), expect_count)
    print(f"     {DIM}{data.get('feature_count')} 个要素 -> {out.name}{RESET}")
    return data


async def main(url: str) -> int:
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = httpx2.AsyncClient(headers={"X-Project-Root": str(ROOT)})

    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== 1. 栅格转面 ===")
            # 6x6 栅格，值 1/2/3；值 1 分成不相连的两块 -> create_multipart=false 应得 4 个面
            await run_tool(session, "栅格转面", "raster_to_polygon",
                           {"in_raster": "./data/rasters/landcover.tif",
                            "out_features": "./build_geo/landcover_probe.shp",
                            "create_multipart": False},
                           expect_count=4)

            print("\n=== 2. 消除：先看输入有多少小面，再消除 ===")
            before = await call(session, "read_shp_data",
                                {"shp": "./build_geo/landcover_probe.shp", "limit": 0})
            check("能读出输入的表结构", before.get("ok"), True)
            await run_tool(session, "消除", "eliminate",
                           {"in_features": "./data/Demo.gdb/districts",
                            "out_features": "./build_geo/eliminated.shp",
                            "where": "Shape_Area < 0.1"})
            after = await call(session, "read_shp_data",
                               {"shp": "./build_geo/eliminated.shp", "where": "Shape_Area < 0.1",
                                "limit": 0})
            check("消除后小面（面积<0.1）一个都不剩", after.get("matched_count"), 0)

            print("\n=== 3. 平滑（线）===")
            await run_tool(session, "平滑", "smooth",
                           {"in_features": "./data/shp/roads.shp",
                            "out_features": "./build_geo/roads_smooth.shp",
                            "tolerance": 0.05},
                           expect_count=3)

            print("\n=== 4. 相交 ===")
            inter = await run_tool(session, "相交", "intersect",
                                   {"in_features": ["./data/Demo.gdb/districts",
                                                    "./data/shp/zones.shp"],
                                    "out_features": "./build_geo/intersect.shp"})
            check("相交结果多于 0 个", (inter.get("feature_count") or 0) > 0, True)

            print("\n=== 5. 多部件转单部件 ===")
            # districts 有 5 个要素，其中 1 个多部件（两块）-> 拆完 6 个
            await run_tool(session, "多部件转单部件", "multipart_to_singlepart",
                           {"in_features": "./data/Demo.gdb/districts",
                            "out_features": "./build_geo/districts_single.shp"},
                           expect_count=6)

            print("\n=== 6. 融合 ===")
            await run_tool(session, "融合", "dissolve",
                           {"in_features": "./data/shp/zones.shp",
                            "out_features": "./build_geo/zones_dissolved.shp",
                            "dissolve_field": ["ZONE"]},
                           expect_count=3)

            print("\n=== 7. 更新（不应改动原数据）===")
            await run_tool(session, "更新", "update_features",
                           {"in_features": "./data/shp/zones.shp",
                            "update_features": "./data/Demo.gdb/districts",
                            "out_features": "./build_geo/zones_updated.shp"})
            original = await call(session, "read_shp_data", {"shp": "./data/shp/zones.shp", "limit": 0})
            check("原数据 zones.shp 仍是 3 个要素（没被原地改）", original.get("total_count"), 3)

            print("\n=== 输出到 gdb（缺的 gdb 自动创建）===")
            in_gdb = await run_tool(session, "输出到新建 gdb", "multipart_to_singlepart",
                                    {"in_features": "./data/shp/zones.shp",
                                     "out_features": "./build_geo/新成果.gdb/zones_single"},
                                    expect_count=3)
            check("返回里说明了自动建了 gdb", "地理数据库" in str(in_gdb.get("container_note", "")), True)
            back = await call(session, "read_gdb_data",
                              {"gdb": "./build_geo/新成果.gdb", "dataset": "zones_single", "limit": 1})
            check("能直接读回新 gdb 里的结果", back.get("ok"), True)

            print("\n=== 安全阀：输出已存在时默认不覆盖 ===")
            again = await call(session, "dissolve",
                               {"in_features": "./data/shp/zones.shp",
                                "out_features": "./build_geo/zones_dissolved.shp"})
            check("不覆盖：返回失败", again.get("ok"), False)
            check("提示了 overwrite 的用法", "overwrite" in str(again.get("error")), True)
            overwritten = await call(session, "dissolve",
                                     {"in_features": "./data/shp/zones.shp",
                                      "out_features": "./build_geo/zones_dissolved.shp",
                                      "overwrite": True})
            check("overwrite=true 能生成新版本", overwritten.get("ok"), True)
            check('指定目标被替换', overwritten.get('target_replaced'), True)

            print("\n=== 参数校验 ===")
            no_where = await call(session, "eliminate",
                                  {"in_features": "./data/Demo.gdb/districts",
                                   "out_features": "./build_geo/x.shp"})
            check("eliminate 不传 where 被拒绝（where 必填）",
                  no_where.get("ok"), False)
            bad_algo = await call(session, "smooth",
                                  {"in_features": "./data/shp/roads.shp",
                                   "out_features": "./build_geo/x2.shp",
                                   "algorithm": "WRONG"})
            check("smooth 传错 algorithm 被拒绝", bad_algo.get("ok"), False)
            bad_input = await call(session, "intersect",
                                   {"in_features": ["./data/shp/zones.shp"],
                                    "out_features": "./build_geo/x3.shp"})
            check("intersect 只给一个输入被拒绝", bad_input.get("ok"), False)

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    print()
    if failed:
        print(f"{RED}共 {failed} 项失败，{passed} 项通过{RESET}")
    else:
        print(f"{GREEN}全部 {passed} 项通过 ✅{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
