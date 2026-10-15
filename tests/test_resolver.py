"""路径解析（resolver）的快速单元测试，不需要 arcpy、不需要启动服务。

运行：
    .venv\\Scripts\\python.exe tests\\test_resolver.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arcgis_mcp.core.config import load_settings  # noqa: E402
from arcgis_mcp.core.project import ProjectRegistry  # noqa: E402
from arcgis_mcp.core.resolver import (  # noqa: E402
    ResolutionError,
    list_shp_files,
    resolve_gdb,
    resolve_shp_file,
    resolve_shp_folder,
    split_gdb_dataset_ref,
)

PASSED = 0
FAILED = 0


def check(label: str, got, expected) -> None:
    global PASSED, FAILED
    if got == expected:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}\n       期望: {expected}\n       实际: {got}")


def expect_error(label: str, fn, keyword: str) -> None:
    global PASSED, FAILED
    try:
        fn()
    except ResolutionError as exc:
        if keyword in str(exc):
            PASSED += 1
            print(f"  ok   {label}（报错信息含 {keyword!r}）")
        else:
            FAILED += 1
            print(f"  FAIL {label}：报错信息里没有 {keyword!r}\n       {exc}")
    else:
        FAILED += 1
        print(f"  FAIL {label}：本应报错却成功了")


def main() -> int:
    # 用项目作用域拿到「本仓库」的数据源（demo / shp_data 定义在 .arcgis-mcp 里）
    settings = ProjectRegistry(load_settings(), mode="http").view(header=str(ROOT)).settings
    gdb = Path(settings.workspaces["demo"].path)
    shp_dir = Path(settings.workspaces["shp_data"].path)

    print("别名解析：")
    check("gdb 别名 demo", resolve_gdb("demo", settings), gdb)
    check("gdb 别名大小写不敏感 DEMO", resolve_gdb("DEMO", settings), gdb)
    check("gdb 用库名 Demo.gdb", resolve_gdb("Demo.gdb", settings), gdb)
    check("gdb 用绝对路径", resolve_gdb(str(gdb), settings), gdb)
    check("shp 目录别名 shp_data", resolve_shp_folder("shp_data", settings), shp_dir)

    print("\ngdb / 数据集 拆分：")
    check('"Demo.gdb/roads" -> 拆出数据集',
          split_gdb_dataset_ref("Demo.gdb/roads", None, settings), ("Demo.gdb", "roads"))
    check('"demo/cities" -> 拆出数据集',
          split_gdb_dataset_ref("demo/cities", None, settings), ("demo", "cities"))
    check("绝对路径 + 数据集",
          split_gdb_dataset_ref(str(gdb) + "/cities", None, settings), (str(gdb), "cities"))
    check("纯路径不拆分", split_gdb_dataset_ref(str(gdb), None, settings), (str(gdb), ""))
    check("已给 dataset 时以参数为准",
          split_gdb_dataset_ref("demo/other", "cities", settings), ("demo", "cities"))

    print("\nshapefile 解析：")
    check("别名目录 + 名字", resolve_shp_file("roads", "shp_data", settings), shp_dir / "roads.shp")
    check("带 .shp 后缀", resolve_shp_file("roads.shp", "shp_data", settings), shp_dir / "roads.shp")
    check("大小写不敏感", resolve_shp_file("ROADS", "shp_data", settings), shp_dir / "roads.shp")
    check("不填 folder，递归在搜索根里找到子目录的 poi",
          resolve_shp_file("poi", None, settings), shp_dir / "points" / "poi.shp")
    check("别名/名字 写法", resolve_shp_file("shp_data/roads", None, settings), shp_dir / "roads.shp")
    check("直接给完整 .shp 路径", resolve_shp_file(str(shp_dir / "roads.shp"), None, settings),
          shp_dir / "roads.shp")

    print("\n列目录：")
    check("非递归只看当前层", [p.name for p in list_shp_files("shp_data", settings)],
          ["roads.shp", "soils.shp", "zones.shp"])
    check("递归包含子目录",
          sorted(p.name for p in list_shp_files("shp_data", settings, recursive=True)),
          ["poi.shp", "roads.shp", "soils.shp", "zones.shp"])

    print("\n报错信息要能指导 AI 自我纠正：")
    expect_error("不存在的 gdb 提示可用别名",
                 lambda: resolve_gdb("no_such", settings), "配置里可用的别名")
    expect_error("不存在的 shp 提示候选",
                 lambda: resolve_shp_file("nope", "shp_data", settings), "找不到")
    expect_error("类型用错（拿 shp 目录当 gdb）",
                 lambda: resolve_gdb("shp_data", settings), "不是地理数据库")

    print()
    print("./ 相对路径按项目根目录解析（最常用写法）：")
    check("./data/Demo.gdb", resolve_gdb("./data/Demo.gdb", settings), gdb)
    check("data/Demo.gdb（不写 ./ 也一样）", resolve_gdb("data/Demo.gdb", settings), gdb)
    check("./data/shp/roads.shp",
          resolve_shp_file("./data/shp/roads.shp", None, settings), shp_dir / "roads.shp")
    check("./data/shp/roads（不带后缀）",
          resolve_shp_file("./data/shp/roads", None, settings), shp_dir / "roads.shp")
    check("./data/shp/points/poi.shp（子目录）",
          resolve_shp_file("./data/shp/points/poi.shp", None, settings), shp_dir / "points" / "poi.shp")
    check("./data/shp 当目录用", resolve_shp_folder("./data/shp", settings), shp_dir)
    check("路径里夹带要素类会被拆出来",
          split_gdb_dataset_ref("./data/Demo.gdb/cities", None, settings),
          (os.path.join(".", "data", "Demo.gdb"), "cities"))

    print()
    print("服务进程的 cwd 不许抢项目根目录（回归）：")
    tmp = Path(tempfile.mkdtemp(prefix="arcgis-mcp-decoy-"))
    old_cwd = Path.cwd()
    try:
        # 在 cwd 里造一个同名的假 gdb，模拟「服务进程碰巧在别的目录」
        (tmp / "data" / "Demo.gdb").mkdir(parents=True)
        os.chdir(tmp)
        project_settings = ProjectRegistry(load_settings(), mode="http").view(header=str(ROOT)).settings
        check("./data/Demo.gdb 仍解析到项目里的那个",
              resolve_gdb("./data/Demo.gdb", project_settings), gdb)
        check("data/Demo.gdb 也优先项目根目录",
              resolve_gdb("data/Demo.gdb", project_settings), gdb)
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print(f"共 {FAILED} 项失败，{PASSED} 项通过")
    else:
        print(f"全部 {PASSED} 项通过 ✅")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
