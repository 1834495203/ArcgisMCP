"""端到端跑通用户那条真实工作流（在样例数据上等价复现）。

工作流（用户原话 → 对应本服务的工具）：
    1. 栅格转面                              raster_to_polygon
    2. （先算面积字段）消除 15 亩图斑两次     add_field + calculate_fields + eliminate ×2
    3. 平滑两次 → A                          smooth ×2
    4. 把 B（土壤）中的寒冻土导出             select
    5. 用寒冻土图斑相交 A → C                 intersect
    6. 转单部件                              multipart_to_singlepart
    7. （重算面积）消除 15 亩图斑两次 → D     calculate_fields + eliminate ×2
    8. 给 D 赋值（gridcode=0 的赋 TL/YL/TS/TZ）calculate_fields
    9. 用 D 更新 B                            update
   10. 再按 TL/YL/TS/TZ 融合                 dissolve

样例数据的栅格特意做成「单格约 11 亩、3×3 块约 100 亩」，所以 where 里的 15 亩就是原话那个阈值；
所以这里按面积大小用等价的面积阈值（Shape_Area < 0.05 等）代替，
并用 gridcode 处理 0 值那一片 —— 步骤和工具串法完全一致。

先启动服务：scripts\\run-http.bat
再运行：.venv\\Scripts\\python.exe tests\\workflow_check.py
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
WORK = ROOT / "build_wf"
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


def output_exists(out: str) -> bool:
    p = Path(out)
    return p.exists() or any(part.lower().endswith((".gdb", ".mdb")) for part in p.parts[:-1])


async def step(session: ClientSession, label: str, tool: str, args: dict, **checks) -> dict:
    data = await call(session, tool, args)
    ok = data.get("ok")
    check(f"{label}（{tool}）执行成功", ok, True)
    if not ok:
        print(f"     {DIM}{str(data.get('error'))[:300]}{RESET}")
        return data
    if "out_features" in data:
        check(f"{label} 输出落盘", output_exists(data["out_features"]), True)
    for key, expected in checks.items():
        check(f"{label} {key}={expected}", data.get(key), expected)
    detail = data.get("feature_count", data.get("updated_count", ""))
    print(f"     {DIM}{detail} {data.get('shape_type') or ''}{RESET}")
    return data


async def main(url: str) -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "out").mkdir(parents=True)
    out = "./build_wf/out"
    client = httpx2.AsyncClient(headers={"X-Project-Root": str(ROOT)})

    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== 1. 栅格转面 ===")
            await step(session, "栅格转面", "raster_to_polygon",
                       {"in_raster": "./data/rasters/landcover.tif",
                        "out_features": f"{out}/A0.shp", "create_multipart": False},
                       feature_count=4)   # 值 0/1/2 各一块 + 值 3 一个小格子

            print("")
            print("=== 2. 算面积（亩）后消除 15 亩以下图斑两次 ===")
            # RasterToPolygon 出来的 shapefile 没有面积字段，必须先建字段再算（真实数据也一样）
            area_added = await step(session, "加面积字段", "add_field",
                       {"in_features": f"{out}/A0.shp", "field": "AREA_MU", "field_type": "DOUBLE"})
            area_calculated = await step(session, "算面积（亩）", "calculate_fields",
                       {"in_features": area_added['output'],
                        "assignments": {"AREA_MU": "!shape.geodesicArea@SQUAREMETERS! / 666.6667"}})
            await step(session, "消除第 1 遍（小于 15 亩）", "eliminate",
                       {"in_features": area_calculated['output'], "out_features": f"{out}/A1.shp",
                        "where": "AREA_MU < 15"},
                       eliminated_count=1, feature_count=3)
            # 第 2 遍已经没有小于 15 亩的图斑：这是「安全空操作」，不该报错
            await step(session, "消除第 2 遍（已无小图斑，空操作）", "eliminate",
                       {"in_features": f"{out}/A1.shp", "out_features": f"{out}/A2.shp",
                        "where": "AREA_MU < 15"},
                       eliminated_count=0, feature_count=3)

            print("\n=== 3. 平滑两次 -> A ===")
            await step(session, "平滑第 1 遍", "smooth",
                       {"in_features": f"{out}/A2.shp", "out_features": f"{out}/A_s1.shp",
                        "tolerance": 0.01})
            await step(session, "平滑第 2 遍", "smooth",
                       {"in_features": f"{out}/A_s1.shp", "out_features": f"{out}/A.shp",
                        "tolerance": 0.01})

            print("\n=== 4. 把 B（土壤）里的寒冻土导出 ===")
            await step(session, "导出寒冻土", "select",
                       {"in_features": "./data/shp/soils.shp",
                        "out_features": f"{out}/寒冻土.shp", "where": "TYPE = '寒冻土'"},
                       feature_count=1)

            print("\n=== 5. 寒冻土 ∩ A -> C ===")
            c = await step(session, "相交", "intersect",
                           {"in_features": [f"{out}/寒冻土.shp", f"{out}/A.shp"],
                            "out_features": f"{out}/C.shp"})
            check("C 里带着 A 的 gridcode 字段", "gridcode" in str(c.get("fields")), True)

            print("\n=== 6. 转单部件 ===")
            await step(session, "转单部件", "multipart_to_singlepart",
                       {"in_features": f"{out}/C.shp", "out_features": f"{out}/C_s.shp"})

            print("")
            print("=== 7. 重算面积后按 15 亩消除两次 -> D ===")
            # 相交/拆单部件后面积变了，重算一遍（真实工作流里必须这么做）
            recalculated = await step(session, "重算面积（亩）", "calculate_fields",
                       {"in_features": f"{out}/C_s.shp",
                        "assignments": {"AREA_MU": "!shape.geodesicArea@SQUAREMETERS! / 666.6667"}})
            await step(session, "消除第 1 遍", "eliminate",
                       {"in_features": recalculated['output'], "out_features": f"{out}/D1.shp",
                        "where": "AREA_MU < 15"})
            await step(session, "消除第 2 遍", "eliminate",
                       {"in_features": f"{out}/D1.shp", "out_features": f"{out}/D.shp",
                        "where": "AREA_MU < 15"})

            print("\n=== 8. 给 D 赋值：gridcode 为 0 的图斑赋 TL/YL/TS/TZ ===")
            assigned = await step(session, "批量赋值", "calculate_fields",
                                  {"in_features": f"{out}/D.shp",
                                   "assignments": {"TL": "'草毡土'",
                                                   "YL": "'薄草毡土'",
                                                   "TS": "'砾砂薄草毡土'"},
                                   "where": "gridcode = 0"})
            check("有要素被赋值（gridcode=0 的图斑存在）",
                  (assigned.get("updated_count") or 0) > 0, True)
            verify = await call(session, "read_shp_data",
                                {"shp": assigned['output'], "where": "TL = '草毡土'",
                                 "fields": ["TL", "YL", "TS"], "limit": 3})
            check("回读确认 TL 已写入", (verify.get("returned") or 0) > 0, True)
            if verify.get("records"):
                print(f"     {DIM}{verify['records'][0]}{RESET}")

            print("\n=== 9. 用 D 去更新 B（不原地改 B）===")
            updated = await step(session, "更新", "update_features",
                                 {"in_features": "./data/shp/soils.shp",
                                  "update_features": assigned['output'],
                                  "out_features": f"{out}/B_updated.shp"})
            check("更新结果带着赋值后的 TL 字段",
                  "TL" in str(updated.get("fields")), True)
            origin = await call(session, "read_shp_data",
                                {"shp": "./data/shp/soils.shp", "where": "TL = '草毡土'", "limit": 0})
            check("原 B（soils.shp）没被改动", origin.get("matched_count"), 0)

            print("\n=== 10. 按 TL YL TS TZ 融合 ===")
            await step(session, "融合", "dissolve",
                       {"in_features": f"{out}/B_updated.shp", "out_features": f"{out}/E.shp",
                        "dissolve_field": ["TL", "YL", "TS", "TZ"]})

            print("\n=== 安全阀：输出和输入是同一个数据集时必须拦下 ===")
            same = await call(session, "dissolve",
                              {"in_features": f"{out}/E.shp",
                               "out_features": f"{out}/E.shp",
                               "dissolve_field": ["TL"], "overwrite": True})
            check("同路径输出被拒绝（否则会删掉输入）", same.get("ok"), False)
            check("报错说明了原因", "同一个数据集" in str(same.get("error")), True)

    shutil.rmtree(WORK, ignore_errors=True)
    print()
    if failed:
        print(f"{RED}共 {failed} 项失败，{passed} 项通过{RESET}")
    else:
        print(f"{GREEN}全部 {passed} 项通过 ✅{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
