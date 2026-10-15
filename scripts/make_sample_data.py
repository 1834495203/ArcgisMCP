"""生成示例数据，用来端到端验证两个读取工具。

必须用 ArcGIS Pro 自带的 Python 运行：
    "D:\\software\\arcgispro\\bin\\Python\\envs\\arcgispro-py3\\python.exe" scripts\\make_sample_data.py

产出：
    data/Demo.gdb               要素类 cities(POINT)、要素数据集 base/rivers(POLYLINE)、表 notes
    data/shp/roads.shp          POLYLINE
    data/shp/points/poi.shp     POINT（放子目录，用来验证按名字搜索）
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

import arcpy

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
GDB = DATA / "Demo.gdb"
SHP_DIR = DATA / "shp"

WGS84 = arcpy.SpatialReference(4326)

CITIES = [
    ("Beijing", 21540000, 16410.54, datetime.datetime(2024, 1, 1)),
    ("Shanghai", 24870000, 6340.5, datetime.datetime(2024, 1, 2)),
    ("Guangzhou", 18680000, 7434.4, datetime.datetime(2024, 2, 11)),
    ("Chengdu", 20940000, 14335.0, datetime.datetime(2024, 3, 5)),
    ("Xian", 12950000, 10752.0, datetime.datetime(2024, 4, 9)),
]

RIVERS = [
    ("Yangtze", 6300.0, [[(91.0, 33.5), (104.0, 30.6), (114.3, 30.6), (121.5, 31.4)]]),
    ("Yellow", 5464.0, [[(96.0, 35.0), (103.8, 36.1), (110.0, 34.8), (119.0, 37.8)]]),
]

POI = [
    ("Forbidden City", "landmark", (116.397, 39.916)),
    ("The Bund", "landmark", (121.490, 31.240)),
    ("Chunxi Road", "shopping", (104.081, 30.657)),
]

ROADS = [
    ("G6 Expressway", 6, 371.0, [[(116.30, 39.95), (111.70, 40.80), (101.70, 36.60)]]),
    ("G4 Expressway", 8, 2273.0, [[(116.30, 39.90), (114.50, 38.05), (113.30, 23.15)]]),
    ("S20 Outer Ring", 6, 98.5, [[(121.35, 31.10), (121.60, 31.10), (121.60, 31.35), (121.35, 31.35)]]),
]


def _reset(path: Path) -> None:
    if path.exists():
        if path.is_dir():
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    if path.suffix.lower() == ".shp":
        for ext in (".shx", ".dbf", ".prj", ".cpg", ".sbx", ".shp.xml", ".sbn"):
            side = path.with_suffix(ext)
            if side.exists():
                side.unlink()


def make_gdb() -> None:
    _reset(GDB)
    arcpy.management.CreateFileGDB(str(DATA), GDB.name)
    print(f"已创建 {GDB}")

    cities = os.path.join(str(GDB), "cities")
    arcpy.management.CreateFeatureclass(str(GDB), "cities", "POINT", spatial_reference=WGS84)
    arcpy.management.AddField(cities, "NAME", "TEXT", field_length=50)
    arcpy.management.AddField(cities, "POP", "LONG")
    arcpy.management.AddField(cities, "AREA_KM2", "DOUBLE")
    arcpy.management.AddField(cities, "UPDATED", "DATE")
    with arcpy.da.InsertCursor(cities, ["SHAPE@XY", "NAME", "POP", "AREA_KM2", "UPDATED"]) as cur:
        coords = [(116.407, 39.904), (121.474, 31.230), (113.264, 23.129), (104.066, 30.572), (108.948, 34.263)]
        for (name, pop, area, updated), xy in zip(CITIES, coords):
            cur.insertRow([xy, name, pop, area, updated])
    print(f"  要素类 cities: {len(CITIES)} 条")

    arcpy.management.CreateFeatureDataset(str(GDB), "base", WGS84)
    rivers = os.path.join(str(GDB), "base", "rivers")
    arcpy.management.CreateFeatureclass(os.path.join(str(GDB), "base"), "rivers", "POLYLINE",
                                        spatial_reference=WGS84)
    arcpy.management.AddField(rivers, "NAME", "TEXT", field_length=50)
    arcpy.management.AddField(rivers, "LENGTH_KM", "DOUBLE")
    with arcpy.da.InsertCursor(rivers, ["SHAPE@", "NAME", "LENGTH_KM"]) as cur:
        for name, length, parts in RIVERS:
            for part in parts:
                cur.insertRow([arcpy.Polyline(arcpy.Array([arcpy.Point(*p) for p in part]), WGS84), name, length])
    print(f"  要素数据集 base/rivers: {len(RIVERS)} 条")

    notes = os.path.join(str(GDB), "notes")
    arcpy.management.CreateTable(str(GDB), "notes")
    arcpy.management.AddField(notes, "TITLE", "TEXT", field_length=80)
    arcpy.management.AddField(notes, "BODY", "TEXT", field_length=254)
    arcpy.management.AddField(notes, "CREATED", "DATE")
    with arcpy.da.InsertCursor(notes, ["TITLE", "BODY", "CREATED"]) as cur:
        cur.insertRow(["示例数据", "本 gdb 由 scripts/make_sample_data.py 生成", datetime.datetime(2024, 5, 1)])
        cur.insertRow(["坐标系", "全部数据使用 WGS84 (EPSG:4326)", datetime.datetime(2024, 5, 2)])
    print("  表 notes: 2 条")


def make_shp() -> None:
    SHP_DIR.mkdir(parents=True, exist_ok=True)

    # 注意：arcpy 3.x 的 gp 工具只接受字符串路径，传 Path 对象会报 "Object: 执行工具时出错"
    roads = str(SHP_DIR / "roads.shp")
    _reset(Path(roads))
    arcpy.management.CreateFeatureclass(str(SHP_DIR), "roads", "POLYLINE", spatial_reference=WGS84)
    arcpy.management.AddField(roads, "NAME", "TEXT", field_length=40)
    arcpy.management.AddField(roads, "LANES", "SHORT")
    arcpy.management.AddField(roads, "LENGTH_KM", "DOUBLE")
    with arcpy.da.InsertCursor(roads, ["SHAPE@", "NAME", "LANES", "LENGTH_KM"]) as cur:
        for name, lanes, length, parts in ROADS:
            for part in parts:
                cur.insertRow([arcpy.Polyline(arcpy.Array([arcpy.Point(*p) for p in part]), WGS84),
                               name, lanes, length])
    print(f"已创建 {roads}: {len(ROADS)} 条")

    points_dir = SHP_DIR / "points"
    points_dir.mkdir(parents=True, exist_ok=True)
    poi = str(points_dir / "poi.shp")
    _reset(Path(poi))
    arcpy.management.CreateFeatureclass(str(points_dir), "poi", "POINT", spatial_reference=WGS84)
    arcpy.management.AddField(poi, "NAME", "TEXT", field_length=40)
    arcpy.management.AddField(poi, "TYPE", "TEXT", field_length=20)
    with arcpy.da.InsertCursor(poi, ["SHAPE@XY", "NAME", "TYPE"]) as cur:
        for name, kind, xy in POI:
            cur.insertRow([xy, name, kind])
    print(f"已创建 {poi}: {len(POI)} 条")


def _rect(x1: float, y1: float, x2: float, y2: float) -> arcpy.Polygon:
    ring = arcpy.Array([arcpy.Point(x1, y1), arcpy.Point(x2, y1),
                        arcpy.Point(x2, y2), arcpy.Point(x1, y2), arcpy.Point(x1, y1)])
    return arcpy.Polygon(ring, WGS84)


def make_polygons() -> None:
    """面数据：给相交 / 融合 / 消除 / 多部件转单部件这些分析工具用。

    districts（在 Demo.gdb 里）特意包含：
      - 3 个相邻大面（融合、相交用）
      - 1 个很小的面（消除用，面积 < 0.1）
      - 1 个多部件要素（两块不相连的正方形，多部件转单部件用）
    """
    districts = str(GDB / "districts")
    arcpy.management.CreateFeatureclass(str(GDB), "districts", "POLYGON", spatial_reference=WGS84)
    arcpy.management.AddField(districts, "NAME", "TEXT", field_length=40)
    arcpy.management.AddField(districts, "ZONE", "TEXT", field_length=20)
    with arcpy.da.InsertCursor(districts, ["SHAPE@", "NAME", "ZONE"]) as cur:
        cur.insertRow([_rect(100, 20, 110, 30), "district_a", "north"])
        cur.insertRow([_rect(110, 20, 120, 30), "district_b", "north"])
        cur.insertRow([_rect(100, 30, 110, 40), "district_c", "south"])
        cur.insertRow([_rect(110.0, 30.0, 110.05, 30.05), "sliver", "south"])   # 面积约 0.0025
        # 多部件要素：两块不相连的正方形
        multi = arcpy.Array([arcpy.Array([arcpy.Point(121, 20), arcpy.Point(123, 20),
                                          arcpy.Point(123, 22), arcpy.Point(121, 22),
                                          arcpy.Point(121, 20)]),
                             arcpy.Array([arcpy.Point(125, 20), arcpy.Point(127, 20),
                                          arcpy.Point(127, 22), arcpy.Point(125, 22),
                                          arcpy.Point(125, 20)])])
        cur.insertRow([arcpy.Polygon(multi, WGS84), "multipart", "east"])
    print("  要素类 districts: 5 个要素（其中 1 个多部件、1 个细小面）")

    zones = str(SHP_DIR / "zones.shp")
    _reset(Path(zones))
    arcpy.management.CreateFeatureclass(str(SHP_DIR), "zones", "POLYGON", spatial_reference=WGS84)
    arcpy.management.AddField(zones, "NAME", "TEXT", field_length=40)
    arcpy.management.AddField(zones, "ZONE", "TEXT", field_length=20)
    with arcpy.da.InsertCursor(zones, ["SHAPE@", "NAME", "ZONE"]) as cur:
        cur.insertRow([_rect(105, 15, 115, 25), "zone_west", "north"])
        cur.insertRow([_rect(95, 25, 105, 35), "zone_east", "south"])
        cur.insertRow([_rect(112, 22, 130, 28), "zone_multi", "east"])
    print(f"已创建 {zones}: 3 个面")


def make_soils() -> None:
    """土壤图层（工作流里的 B）：带 TYPE 分类字段，以及要赋值的 TL/YL/TS/TZ 四个字段。"""
    soils = str(SHP_DIR / "soils.shp")
    _reset(Path(soils))
    arcpy.management.CreateFeatureclass(str(SHP_DIR), "soils", "POLYGON", spatial_reference=WGS84)
    arcpy.management.AddField(soils, "TYPE", "TEXT", field_length=20)
    for name in ("TL", "YL", "TS", "TZ"):
        arcpy.management.AddField(soils, name, "TEXT", field_length=40)
    with arcpy.da.InsertCursor(soils, ["SHAPE@", "TYPE"]) as cur:
        cur.insertRow([_rect(100, 20, 120, 40), "寒冻土"])     # 覆盖 districts 主区域
        cur.insertRow([_rect(120, 20, 130, 30), "草毡土"])
        cur.insertRow([_rect(95, 25, 105, 35), "沼泽土"])
    print(f"已创建 {soils}: 3 个面（TYPE 含 寒冻土/草毡土/沼泽土，另带 TL/YL/TS/TZ 空字段）")


def make_raster() -> None:
    """栅格数据：给「栅格转面」用。1/2/3 三个值分成 4 个连通区域。"""
    import numpy as np

    raster_dir = DATA / "rasters"
    raster_dir.mkdir(parents=True, exist_ok=True)
    target = raster_dir / "landcover.tif"
    _reset(target)

    values = np.array([
        [0, 0, 0, 1, 1, 1],
        [0, 0, 0, 1, 1, 1],
        [0, 0, 0, 1, 1, 1],
        [2, 2, 2, 2, 2, 2],
        [2, 2, 2, 2, 2, 2],
        [3, 2, 2, 2, 2, 2],
    ], dtype="int16")
    # 放在寒冻土范围内（lon 100, lat 20），格子边长 0.0008 度
    # （约 84m × 88m ≈ 7400 m² ≈ 11 亩）：单个格子的面小于 15 亩、3×3 的块约 100 亩，
    # 这样「消除 15 亩以下的图斑」在样例数据上就有真实效果
    # （亩 = 666.6667 平方米；用 !shape.geodesicArea@SQUAREMETERS! 算真面积）。
    raster = arcpy.NumPyArrayToRaster(values, arcpy.Point(100, 20), 0.0008, 0.0008)
    raster.save(str(target))
    arcpy.management.DefineProjection(str(target), WGS84)
    print(f"已创建 {target}: 6x6 栅格，值 0/1/2/3（0 和 1 各占 3x3，3 只有 1 个格子；"
          f"1 格约 11 亩、3x3 约 100 亩）")


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"输出目录：{DATA}")
    make_gdb()
    make_shp()
    make_polygons()
    make_soils()
    make_raster()
    print("示例数据生成完成 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
