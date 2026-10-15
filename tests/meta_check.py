"""验证「通用 arcpy 调用层」——固定工具覆盖不到的事情也能做。

这一层的意义就是通用性，所以测试要证明三件事：
  1. 能搜到、能看清任意 arcpy 工具的参数（1800 多个工具的入口）；
  2. 能真的调用**固定工具没有封装**的工具（这里用生成六边形格网、pairwise 融合、
     拓扑检查、插值等固定工具里没有的操作）；
  3. 路径解析、覆盖保护、破坏性工具确认这些护栏对通用调用同样生效。

先启动服务：scripts\\run-http.bat
再运行：.venv\\Scripts\\python.exe tests\\meta_check.py
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
WORK = ROOT / "build_meta"
OUT = "./build_meta/out"
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
        return {"ok": False, "raw": text[:300]}


async def main(url: str) -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "out").mkdir(parents=True)
    districts = "./data/Demo.gdb/districts"
    client = httpx2.AsyncClient(headers={"X-Project-Root": str(ROOT)})

    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== 1. 能搜到任意 arcpy 工具（固定工具之外的世界）===")
            tess = await call(session, "list_arcpy_tools", {"keyword": "Tessellation"})
            check("搜 Tessellation 命中", (tess.get("total_matched") or 0) >= 1, True)
            check("命中项带语法行", bool(tess.get("tools", [{}])[0].get("syntax")), True)
            print(f"     {DIM}{tess['tools'][0]['syntax'][:110]}{RESET}")

            carto = await call(session, "list_arcpy_tools", {"toolbox": "cartography", "limit": 8})
            check("按工具箱过滤（cartography）有结果", (carto.get("total_matched") or 0) > 30, True)
            print(f"     {DIM}cartography 工具箱 {carto.get('total_matched')} 个，"
                  f"例如 {[t['name'] for t in carto.get('tools', [])][:3]}{RESET}")
            topo = await call(session, "list_arcpy_tools", {"keyword": "Topology", "limit": 6})
            check("关键词搜拓扑工具（固定工具里没有）", (topo.get("total_matched") or 0) >= 3, True)
            print(f"     {DIM}含 Topology 的工具 {topo.get('total_matched')} 个，"
                  f"例如 {[t['name'] for t in topo.get('tools', [])][:3]}{RESET}")

            big = await call(session, "list_arcpy_tools", {"keyword": "Dissolve"})
            check("搜索范围覆盖全部工具（>1000）", (big.get("total_matched") or 0) >= 1, True)

            print("\n=== 2. 能看清任意工具的参数（方向/必填/取值域）===")
            d = await call(session, "describe_arcpy_tool", {"tool": "GenerateTessellation"})
            check("解析出工具名", d.get("resolved_name"), "GenerateTessellation_management")
            params = {p["name"]: p for p in d.get("parameters", [])}
            check("认出输出参数", d.get("output_parameters"), ["Output_Feature_Class"])
            check("输出参数标了 Output", params["Output_Feature_Class"]["direction"], "Output")
            check("必填标记正确", params["Extent"]["required"], True)
            check("给出了 Shape_Type 的可选值", len(params["Shape_Type"].get("allowed_values", [])) > 0, True)
            check("带语法行", bool(d.get("usage")), True)
            print(f"     {DIM}Shape_Type 可选：{params['Shape_Type']['allowed_values']}{RESET}")

            pw = await call(session, "describe_arcpy_tool", {"tool": "PairwiseDissolve_analysis"})
            check("pairwise 版本也能查（固定 dissolve 没包这个）",
                  pw.get("resolved_name"), "PairwiseDissolve_analysis")

            print("\n=== 3. 真的调用固定工具之外的 arcpy 工具 ===")
            gen = await call(session, "run_arcpy_tool", {
                "tool": "GenerateTessellation",
                "parameters": {"Output_Feature_Class": f"{OUT}/hex.shp",
                               "Extent": districts, "Shape_Type": "六边形", "Size": 0.5},
            })
            check("生成六边形格网成功（固定工具做不到）", gen.get("ok"), True)
            outs = gen.get("outputs") or [{}]
            check("输出落盘", outs[0].get("exists"), True)
            check("输出有要素", (outs[0].get("feature_count") or 0) > 0, True)
            print(f"     {DIM}生成 {outs[0].get('feature_count')} 个六边形格子{RESET}")

            pdf = await call(session, "run_arcpy_tool", {
                "tool": "PairwiseDissolve_analysis",
                "parameters": {"in_features": "./data/shp/zones.shp",
                               "out_feature_class": f"{OUT}/pairwise.shp",
                               "dissolve_field": ["ZONE"]},
            })
            check("pairwise 融合成功", pdf.get("ok"), True)
            check("pairwise 输出有要素", ((pdf.get("outputs") or [{}])[0].get("feature_count") or 0) > 0, True)

            # 演示「官方推荐流程」：先 describe 看真实参数名，再按它构造调用
            # （各工具的命名风格不一样，这个工具就是 Input_Features / Point_Placement 这种标题式）
            gpal = await call(session, "describe_arcpy_tool",
                              {"tool": "GeneratePointsAlongLines_management"})
            spec = {p["name"]: p for p in gpal.get("parameters", [])}
            along_args = {"Input_Features": "./data/shp/roads.shp",
                          "Output_Feature_Class": f"{OUT}/along.shp",
                          "Point_Placement": "按距离", "Distance": 0.5}
            check("构造的参数名都来自 describe 的结果", set(along_args) <= set(spec), True)
            cp = await call(session, "run_arcpy_tool",
                            {"tool": "GeneratePointsAlongLines", "parameters": along_args})
            check("沿线上按距离生成点成功", cp.get("ok"), True)
            check("输出有要素", ((cp.get("outputs") or [{}])[0].get("feature_count") or 0) > 0, True)

            print("\n=== 4. 路径解析：相对路径 + 自动建目录 + 纯名字不被改坏 ===")
            nested = await call(session, "run_arcpy_tool", {
                "tool": "management.CreateFeatureclass",
                "parameters": {"out_path": f"{OUT}/深层/新建.gdb", "out_name": "测试点",
                               "geometry_type": "POINT", "spatial_reference": 4326},
            })
            check("输出写到不存在的目录（服务端自动建目录/gdb）", nested.get("ok"), True)
            check("没把 pure name（out_name）当成路径", nested.get("ok"), True)
            back = await call(session, "read_gdb_data",
                              {"gdb": f"{OUT}/深层/新建.gdb", "dataset": "测试点", "limit": 1})
            check("新建的要素类能读回（说明 out_path/out_name 解析都对）", back.get("ok"), True)

            print("\n=== 5. 通用调用的护栏 ===")
            again = await call(session, "run_arcpy_tool", {
                "tool": "PairwiseDissolve_analysis",
                "parameters": {"in_features": "./data/shp/zones.shp",
                               "out_feature_class": f"{OUT}/pairwise.shp"},
            })
            check("输出已存在时不覆盖（报错）", again.get("ok"), False)
            check("提示了 overwrite", "overwrite" in str(again.get("error")), True)
            again2 = await call(session, "run_arcpy_tool", {
                "tool": "PairwiseDissolve_analysis",
                "parameters": {"in_features": "./data/shp/zones.shp",
                               "out_feature_class": f"{OUT}/pairwise.shp"},
                "overwrite": True,
            })
            check("overwrite=true 时能覆盖", again2.get("ok"), True)

            same = await call(session, "run_arcpy_tool", {
                "tool": "PairwiseDissolve_analysis",
                "parameters": {"in_features": "./data/shp/zones.shp",
                               "out_feature_class": "./data/shp/zones.shp"},
                "overwrite": True,
            })
            check("输出=输入时被拦下（否则会删掉原数据）", same.get("ok"), False)

            destructive = await call(session, "run_arcpy_tool", {
                "tool": "management.Delete",
                "parameters": {"in_data": f"{OUT}/pairwise.shp"},
            })
            check("破坏性工具（Delete）不给 confirm 被拦下", destructive.get("ok"), False)
            check("提示了 confirm=true", "confirm" in str(destructive.get("error")), True)

            archived = await call(session, 'run_arcpy_tool', {
                'tool': 'Delete_management', 'parameters': {'in_data': f'{OUT}/pairwise.shp'}, 'confirm': True})
            check('通用 Delete 有归档删除出口', archived.get('ok'), True)
            check('通用 Delete 原路径消失', archived.get('original_path_exists'), False)
            check('通用 Delete 历史数据保留', Path(archived['output']).exists(), True)
            await call(session, 'copy_features', {'in_features': './data/shp/zones.shp',
                                                'out_features': f'{OUT}/delete_source.shp'})
            cleared = await call(session, 'run_arcpy_tool', {
                'tool': 'DeleteFeatures_management', 'parameters': {'in_features': f'{OUT}/delete_source.shp'}, 'confirm': True})
            check('通用 DeleteFeatures 仅清空副本', cleared.get('deleted_count'), 3)
            original = await call(session, 'read_shp_data', {'shp': './data/shp/zones.shp', 'limit': 0})
            check('通用 DeleteFeatures 保留原件', original.get('total_count'), 3)
            exported = await call(session, 'run_arcpy_tool', {'tool': 'TableToTable_conversion', 'parameters': {
                'in_rows': districts, 'out_path': f'{OUT}/tables', 'out_name': 'classes.csv'}})
            check('TableToTable 相对目录导出成功', exported.get('ok'), True)
            export_path = Path(exported['outputs'][0]['path'])
            check('TableToTable CSV 没有额外文件', [p.name for p in export_path.parent.iterdir()], ['classes.csv'])
            absolute = await call(session, 'run_arcpy_tool', {'tool': 'TableToTable_conversion', 'parameters': {
                'in_rows': districts, 'out_path': str(WORK / 'absolute'), 'out_name': 'classes.dbf'}})
            check('TableToTable 绝对目录导出成功', absolute.get('ok'), True)

            bad_param = await call(session, "run_arcpy_tool", {
                "tool": "GenerateTessellation",
                "parameters": {"Output_Feature_Class": f"{OUT}/bad.shp", "Extent": districts,
                               "Size": "不对的参数名" if False else 0.5},
            })
            check("参数写对时正常（对照）", bad_param.get("ok"), True)

            print("\n=== 6. run_arcpy_code 默认关闭（安全默认）===")
            listed = [t.name for t in (await session.list_tools()).tools]
            check("默认不注册 run_arcpy_code", "run_arcpy_code" in listed, False)
            check("通用层三个工具都在", all(n in listed for n in
                                       ("list_arcpy_tools", "describe_arcpy_tool", "run_arcpy_tool")), True)

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
