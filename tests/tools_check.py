"""端到端覆盖测试：把 65 个工具逐个真跑一遍。

每个工具都：调用 → 断言 ok → 断言产物落盘（或原地改动的结果可读回）。
会写数据的工具用样例数据当输入，产物都放在 build_tools/ 下，跑完删掉。

先启动服务：scripts\\run-http.bat
再运行：.venv\\Scripts\\python.exe tests\\tools_check.py
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
WORK = ROOT / "build_tools"
OUT = "./build_tools/out"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

passed = 0
failed = 0
COVERED: set = set()


def check(label: str, got, expected) -> None:
    global passed, failed
    if got == expected:
        passed += 1
        print(f"    {GREEN}ok{RESET}   {label}")
    else:
        failed += 1
        print(f"    {RED}FAIL{RESET} {label}\n         期望: {expected}\n         实际: {got}")


async def call(session: ClientSession, name: str, args: dict) -> dict:
    COVERED.add(name)
    result = await session.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        from job_client import completed
        return await completed(session, json.loads(text))
    except json.JSONDecodeError:
        return {"ok": False, "raw": text[:200]}


def exists(out: str) -> bool:
    """产物可能是文件，也可能是 gdb 内的要素类 / 目录。"""
    p = Path(str(out))
    if p.exists():
        return True
    return any(part.lower().endswith((".gdb", ".mdb")) for part in p.parts[:-1])


async def ok(session: ClientSession, label: str, tool: str, args: dict, out_key: str = "out_features",
             **checks) -> dict:
    """跑一个工具，断言成功 + 产物存在 + 附加断言。"""
    data = await call(session, tool, args)
    if not data.get("ok"):
        check(f"{label}（{tool}）", f"失败：{str(data.get('error'))[:160]}", "成功")
        return data
    check(f"{label}（{tool}）成功", True, True)
    output = data.get(out_key)
    if output:
        check(f"{label} 产物落盘", exists(output), True)
    for key, expected in checks.items():
        check(f"{label} {key}={expected}", data.get(key), expected)
    return data


async def main(url: str) -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "out").mkdir(parents=True)
    src = {  # 样例数据
        "gdb": "./data/Demo.gdb", "districts": "./data/Demo.gdb/districts",
        "cities": "./data/Demo.gdb/cities", "rivers": "./data/Demo.gdb/base/rivers",
        "roads": "./data/shp/roads.shp", "zones": "./data/shp/zones.shp",
        "soils": "./data/shp/soils.shp", "poi": "./data/shp/points/poi.shp",
        "raster": "./data/rasters/landcover.tif",
    }
    client = httpx2.AsyncClient(headers={"X-Project-Root": str(ROOT)})

    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = {t.name for t in (await session.list_tools()).tools}
            # 通用层三个工具（list/describe/run_arcpy_tool）由 tests/meta_check.py 专门覆盖
            meta_tools = {"list_arcpy_tools", "describe_arcpy_tool", "run_arcpy_tool",
                          "run_arcpy_code"}
            listed -= meta_tools
            print(f"服务端提供 {len(listed)} 个工具\n")

            # ---------------- 项目与探索 ----------------
            print("=== 项目与探索 ===")
            await ok(session, "看数据源", "list_datasources", {})
            await ok(session, "arcpy 自检", "arcgis_status", {})
            d = await ok(session, "描述面数据", "describe_data", {"dataset": src["districts"]})
            check("describe_data 认出几何类型", d.get("shape_type"), "Polygon")
            dr = await ok(session, "描述栅格", "describe_data", {"dataset": src["raster"]})
            check("describe_data 给出栅格像元大小", isinstance(dr.get("cell_size"), dict), True)
            await ok(session, "use_project", "use_project", {"folder": str(ROOT)}, out_key="project_root")

            print("\n=== 读取 ===")
            await ok(session, "列 gdb", "list_gdb_datasets", {"gdb": src["gdb"]}, out_key="resolved_path")
            await ok(session, "读 gdb", "read_gdb_data",
                     {"gdb": src["gdb"], "dataset": "districts", "limit": 2}, out_key="path")
            await ok(session, "列 shp", "list_shp_datasets", {"folder": "./data/shp"}, out_key="resolved_path")
            await ok(session, "读 shp", "read_shp_data", {"shp": src["soils"], "limit": 2}, out_key="path")

            # ---------------- 分析 ----------------
            print("\n=== 分析：叠加 ===")
            await ok(session, "相交", "intersect",
                     {"in_features": [src["districts"], src["zones"]], "out_features": f"{OUT}/intersect.shp"})
            await ok(session, "联合", "union",
                     {"in_features": [src["districts"], src["zones"]], "out_features": f"{OUT}/union.shp"})
            await ok(session, "擦除", "erase",
                     {"in_features": src["districts"], "erase_features": src["zones"],
                      "out_features": f"{OUT}/erase.shp"})
            await ok(session, "裁剪", "clip",
                     {"in_features": src["zones"], "clip_features": src["districts"],
                      "out_features": f"{OUT}/clip.shp"})
            await ok(session, "标识", "identity",
                     {"in_features": src["districts"], "identity_features": src["zones"],
                      "out_features": f"{OUT}/identity.shp"})
            await ok(session, "对称差", "symmetrical_difference",
                     {"in_features": src["districts"], "update_features": src["zones"],
                      "out_features": f"{OUT}/symdiff.shp"})

            print("\n=== 分析：邻域与选择 ===")
            await ok(session, "缓冲区", "buffer",
                     {"in_features": src["districts"], "out_features": f"{OUT}/buffer.shp",
                      "distance": 0.2, "dissolve_option": "ALL"})
            await ok(session, "空间连接", "spatial_join",
                     {"target_features": src["districts"], "join_features": src["zones"],
                      "out_features": f"{OUT}/spjoin.shp"})
            near = await ok(session, "近邻分析", "near",
                            {"in_features": src["districts"], "near_features": src["cities"],
                             "out_features": f"{OUT}/near.shp"})
            check("near 算出了距离", (near.get("near_count") or 0) > 0, True)
            await ok(session, "按属性筛选", "select",
                     {"in_features": src["soils"], "out_features": f"{OUT}/寒冻土.shp",
                      "where": "TYPE = '寒冻土'"}, feature_count=1)
            await ok(session, "按位置筛选", "select_by_location",
                     {"in_features": src["districts"], "select_features": src["zones"],
                      "out_features": f"{OUT}/sbl.shp"})

            print("\n=== 分析：统计 ===")
            await ok(session, "汇总统计", "summary_statistics",
                     {"in_features": src["districts"], "out_features": f"{OUT}/stats.dbf",
                      "statistics_fields": ["Shape_Area SUM"], "case_field": ["ZONE"]})
            await ok(session, "频数统计", "frequency",
                     {"in_features": src["soils"], "out_features": f"{OUT}/freq.dbf",
                      "frequency_field": ["TYPE"]})

            # ---------------- 数据管理 ----------------
            print("\n=== 数据管理 ===")
            await ok(session, "复制进 gdb", "copy_features",
                     {"in_features": src["roads"], "out_features": f"{WORK}/新库.gdb/roads"})
            await ok(session, "合并", "merge",
                     {"in_features": [src["districts"], src["zones"]], "out_features": f"{OUT}/merge.shp"})
            # 追加：先复制一份当目标，再追加
            await ok(session, "复制（追加目标）", "copy_features",
                     {"in_features": src["soils"], "out_features": f"{OUT}/append_target.shp"})
            appended = await call(session, "append",
                                  {"in_features": f"{OUT}/寒冻土.shp",
                                   "target": f"{OUT}/append_target.shp"})
            check("追加成功", appended.get("ok"), True)
            check("追加后要素数增加 1", appended.get("appended_count"), 1)
            check("追加后总数=4", appended.get("count_after"), 4)
            await ok(session, "投影转换", "project",
                     {"in_features": src["districts"], "out_features": f"{OUT}/projected.shp",
                      "spatial_reference": 3857})
            await ok(session, "定义投影", "define_projection",
                     {"in_features": src["roads"], "out_features": f"{OUT}/defined.shp",
                      "spatial_reference": 4326})
            await ok(session, "几何检查", "check_geometry",
                     {"in_features": src["districts"], "out_features": f"{OUT}/geom_problems.dbf"})
            await ok(session, "创建 gdb", "create_gdb", {"out_features": f"{WORK}/空库.gdb"})
            await ok(session, "创建要素类", "create_feature_class",
                     {"out_features": f"{OUT}/empty.shp", "geometry_type": "POLYGON",
                      "spatial_reference": 4326})
            await ok(session, "创建表", "create_table", {"out_features": f"{OUT}/empty_table.dbf"})
            await ok(session, "要素转点", "feature_to_point",
                     {"in_features": src["districts"], "out_features": f"{OUT}/pts.shp"})
            await ok(session, "要素转线", "feature_to_line",
                     {"in_features": src["districts"], "out_features": f"{OUT}/lines.shp"})
            await ok(session, "最小包围几何", "minimum_bounding_geometry",
                     {"in_features": src["districts"], "out_features": f"{OUT}/hull.shp",
                      "geometry_type": "CONVEX_HULL", "group_option": "ALL"})
            await ok(session, "排序", "sort",
                     {"in_features": src["districts"], "out_features": f"{OUT}/sorted.shp",
                      "sort_field": ["NAME ASC"]})
            await ok(session, "多部件转单部件", "multipart_to_singlepart",
                     {"in_features": src["districts"], "out_features": f"{OUT}/single.shp"},
                     feature_count=6)
            await ok(session, "融合", "dissolve",
                     {"in_features": src["zones"], "out_features": f"{OUT}/dissolved.shp",
                      "dissolve_field": ["ZONE"]}, feature_count=3)
            await ok(session, "消除", "eliminate",
                     {"in_features": src["districts"], "out_features": f"{OUT}/eliminated.shp",
                      "where": "Shape_Area < 0.1"}, eliminated_count=1)
            await ok(session, "更新", "update_features",
                     {"in_features": src["zones"], "update_features": src["districts"],
                      "out_features": f"{OUT}/updated.shp"})

            # ---------------- 字段与几何编辑（原地）----------------
            print("\n=== 字段与几何编辑（原地）===")
            await ok(session, "复制一份改字段", "copy_features",
                     {"in_features": src["soils"], "out_features": f"{OUT}/edit.shp"})
            edit_added = await ok(session, "加字段", "add_field",
                     {"in_features": f"{OUT}/edit.shp", "field": "AREA_MU", "field_type": "DOUBLE"},
                     out_key="in_features", added_field="AREA_MU")
            calc = await ok(session, "批量赋值", "calculate_fields",
                            {"in_features": edit_added['output'],
                             "assignments": {"AREA_MU": "!shape.geodesicArea@SQUAREMETERS! / 666.6667"},
                             "where": "TYPE = '寒冻土'"}, out_key="in_features")
            check("只改了符合条件的要素", calc.get("updated_count"), 1)
            # AlterField 只支持地理数据库数据集，shapefile 会报「数据集的类型不受支持」
            await ok(session, "复制进 gdb（供改字段）", "copy_features",
                     {"in_features": calc['output'], "out_features": f"{WORK}/编辑.gdb/soils"})
            await ok(session, "改字段别名", "alter_field",
                     {"in_features": f"{WORK}/编辑.gdb/soils", "field": "AREA_MU",
                      "new_alias": "面积亩"}, out_key="in_features")
            await ok(session, "删字段", "delete_field",
                     {"in_features": calc['output'], "field": "AREA_MU"}, out_key="in_features")
            await ok(session, "复制点位数据", "copy_features",
                     {"in_features": src["poi"], "out_features": f"{OUT}/poi_xy.shp"})
            xy_added = await ok(session, "加点坐标", "add_xy_coordinates",
                     {"in_features": f"{OUT}/poi_xy.shp"}, out_key="in_features")
            await ok(session, "加几何属性", "add_geometry_attributes",
                     {"in_features": f"{OUT}/single.shp",
                      "geometry_properties": ["AREA", "CENTROID"]}, out_key="in_features")
            await ok(session, "复制一份修几何", "copy_features",
                     {"in_features": src["districts"], "out_features": f"{OUT}/repair.shp"})
            await ok(session, "修复几何", "repair_geometry",
                     {"in_features": f"{OUT}/repair.shp"}, out_key="in_features")
            no_confirm = await call(session, "delete_dataset",
                                    {"in_features": f"{OUT}/repair.shp"})
            check("delete_dataset 不给 confirm 被拒绝", no_confirm.get("ok"), False)
            check("报错说明要 confirm=true", "confirm" in str(no_confirm.get("error")), True)
            # 删除类操作不用通用检查（它的「产物」是数据消失）
            deleted = await call(session, "delete_dataset",
                                 {"in_features": f"{OUT}/repair.shp", "confirm": True})
            check("删除数据集（confirm=true）成功", deleted.get("ok"), True)
            check("逻辑删除保留原件", deleted.get('logical_deleted'), True)
            check("回收副本保留", Path(deleted['output']).exists(), True)
            check('原路径已移除', deleted['original_path_exists'], False)
            await ok(session, '复制删除记录测试数据', 'copy_features',
                     {'in_features': src['soils'], 'out_features': f'{OUT}/delete_source.shp'})
            await ok(session, '在副本中删除记录', 'delete_features',
                     {'in_features': f'{OUT}/delete_source.shp', 'where': "TYPE = '寒冻土'", 'confirm': True}, deleted_count=1)

            # ---------------- 制图综合 ----------------
            print("\n=== 制图综合 ===")
            await ok(session, "平滑", "smooth",
                     {"in_features": src["roads"], "out_features": f"{OUT}/smooth.shp",
                      "tolerance": 0.05})
            await ok(session, "抽稀", "simplify",
                     {"in_features": src["roads"], "out_features": f"{OUT}/simplify.shp",
                      "tolerance": 0.05})
            await ok(session, "加密顶点", "densify",
                     {"in_features": src["roads"], "out_features": f"{OUT}/densify.shp",
                      "distance": 0.5})

            # ---------------- 栅格 ----------------
            print("\n=== 栅格 ===")
            poly = await ok(session, "栅格转面", "raster_to_polygon",
                            {"in_raster": src["raster"], "out_features": f"{OUT}/rpoly.shp",
                             "create_multipart": False}, feature_count=4)
            await ok(session, "栅格转点", "raster_to_point",
                     {"in_raster": src["raster"], "out_features": f"{OUT}/rpoint.shp"})
            await ok(session, "矢量转栅格", "polygon_to_raster",
                     {"in_features": f"{OUT}/rpoly.shp", "out_features": f"{OUT}/back.tif",
                      "value_field": "gridcode", "cell_size": 0.0008})
            await ok(session, "重分类", "reclassify",
                     {"in_raster": src["raster"], "out_features": f"{OUT}/reclass.tif",
                      "remap": "0 1; 1 2; 2 3; 3 4"})
            await ok(session, "按掩膜提取", "extract_by_mask",
                     {"in_raster": src["raster"], "mask": src["districts"],
                      "out_features": f"{OUT}/masked.tif"})
            await ok(session, "复制栅格", "copy_raster",
                     {"in_raster": src["raster"], "out_features": f"{OUT}/copied.tif"})
            await ok(session, "重采样", "resample",
                     {"in_raster": src["raster"], "out_features": f"{OUT}/resampled.tif",
                      "cell_size": 0.0016})
            await ok(session, "栅格投影", "project_raster",
                     {"in_raster": src["raster"], "out_features": f"{OUT}/rproj.tif",
                      "spatial_reference": 3857, "cell_size": 100})
            await ok(session, "分区统计", "zonal_statistics",
                     {"in_features": src["districts"], "in_value_raster": src["raster"],
                      "out_features": f"{OUT}/zonal.dbf", "zone_field": "NAME"})

            # ---------------- 格式转换 ----------------
            print("\n=== 格式转换 ===")
            await ok(session, "导出 Excel", "table_to_excel",
                     {"in_features": src["districts"], "out_features": f"{OUT}/districts.xlsx"})
            await ok(session, "Excel 转表", "excel_to_table",
                     {"in_excel": f"{OUT}/districts.xlsx", "out_features": f"{OUT}/from_excel.dbf"})
            await ok(session, "导出 CSV", "export_table",
                     {"in_features": src["cities"], "out_features": f"{OUT}/cities.csv"})
            js = await ok(session, "要素转 JSON", "features_to_json",
                          {"in_features": src["districts"], "out_features": f"{OUT}/districts.json",
                           "geojson": True})
            # GeoJSON 模式下 arcpy 会强制用 .geojson 后缀，服务端会把真实路径报回来
            json_path = js.get("out_features")
            check("GeoJSON 输出后缀是 .geojson", str(json_path).endswith(".geojson"), True)
            await ok(session, "JSON 转要素", "json_to_features",
                     {"in_json": json_path, "out_features": f"{OUT}/from_json.shp"})
            # 坐标表转点：先用加点坐标的 poi 导成 CSV（有 POINT_X / POINT_Y），再转回点
            await ok(session, "导出坐标表", "export_table",
                     {"in_features": xy_added['output'], "out_features": f"{OUT}/poi_xy.csv"})
            await ok(session, "坐标表转点", "xy_table_to_point",
                     {"in_features": f"{OUT}/poi_xy.csv", "out_features": f"{OUT}/from_csv.shp",
                      "x_field": "POINT_X", "y_field": "POINT_Y", "coordinate_system": 4326},
                     feature_count=3)
            await ok(session, "栅格批量转格式", "raster_to_other_format",
                     {"in_rasters": [src["raster"]], "out_folder": f"{WORK}/栅格导出",
                      "raster_format": "TIFF"}, out_key="out_features")

            # ---------------- 安全阀 ----------------
            print("\n=== 安全阀 ===")
            same = await call(session, "dissolve",
                              {"in_features": f"{OUT}/dissolved.shp",
                               "out_features": f"{OUT}/dissolved.shp", "overwrite": True})
            check("输出=输入被拦下", same.get("ok"), False)
            check("报错说明原因", "同一个数据集" in str(same.get("error")), True)
            exists_again = await call(session, "dissolve",
                                      {"in_features": src["zones"],
                                       "out_features": f"{OUT}/dissolved.shp"})
            check("输出已存在时默认不覆盖", exists_again.get("ok"), False)
            # MCP 对多余参数是静默忽略的（服务端会在日志里 WARNING，客户端看不到），
            # 所以这里断言的是「不影响本次调用」，而不是「被拦下」。
            bad_param = await call(session, "buffer",
                                   {"in_features": src["districts"], "out_features": f"{OUT}/x.shp",
                                    "distance": 0.1, "distanse": 1})
            check("拼错的参数被静默忽略，调用照常成功", bad_param.get("ok"), True)
            missing = await call(session, "buffer",
                                 {"in_features": src["districts"], "out_features": f"{OUT}/x2.shp",
                                  "distnace": 0.1})
            check("漏掉必填参数会被拦下", missing.get("ok"), False)
            bad_op_param = await call(session, "dissolve",
                                      {"in_features": src["zones"],
                                       "out_features": f"{OUT}/x3.shp",
                                       "multi_part": True, "unknown_flag": 1})
            check("工具方法参数写错（multi_part 之外）不影响调用", bad_op_param.get("ok"), True)

            # ---------------- 覆盖率 ----------------
            for query in ('lineage', 'explain'):
                history = await call(session, query, {'path': calc['output']})
                check(query + ' 能查询完整链路', history.get('ok'), True)
            for query in ('job_status', 'job_result'):
                job = await call(session, query, {'job_id': calc['job_id']})
                check(query + ' 返回成功状态', job.get('state'), 'succeeded')
            print("\n=== 工具覆盖率 ===")
            missing = sorted(listed - COVERED)
            check("每个工具都被跑到过", len(missing), 0)
            if missing:
                print(f"     没覆盖到：{missing}")

    shutil.rmtree(WORK, ignore_errors=True)
    print()
    if failed:
        print(f"{RED}共 {failed} 项失败，{passed} 项通过{RESET}")
    else:
        print(f"{GREEN}全部 {passed} 项通过 ✅（覆盖 {len(COVERED)} 个工具）{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
