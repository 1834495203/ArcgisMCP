"""制图综合工具：平滑 / 抽稀 / 加密顶点。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.mcp_compat import Context
from .base import GeoProcessTool


class Smooth(GeoProcessTool):
    """平滑。"""

    name = "smooth"
    operation = "smooth"

    def smooth(self, ctx: Context, in_features: str, out_features: str,
               tolerance: float = 0.01, algorithm: str = "PAEK",
               overwrite: bool = False) -> str:
        """平滑线或面的尖锐拐角（按几何类型自动选线/面算法）。

        tolerance: 容差，**单位与数据一致**。经纬度数据建议 0.001~0.01；
            投影数据按米给（例如 10 = 10 米）。BEZIER 算法实际不用它，但也要给正数。
        algorithm: PAEK（默认，曲线不穿过原顶点，更平滑）或
            BEZIER_INTERPOLATION（穿过原顶点）。
        和 simplify 的区别：平滑是把拐角变圆润，抽稀是去掉多余顶点。
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params={"algorithm": algorithm, "tolerance": float(tolerance)},
                                   overwrite=overwrite)


class Simplify(GeoProcessTool):
    """抽稀。"""

    name = "simplify"
    operation = "simplify"

    def simplify(self, ctx: Context, in_features: str, out_features: str, tolerance: float,
                 algorithm: str = "POINT_REMOVE", overwrite: bool = False) -> str:
        """抽稀（简化）线或面的顶点：去掉不影响整体形状的细节。

        tolerance: **必填**，容差，值越大越简略（单位与数据一致）。
        algorithm: POINT_REMOVE（默认，最常用）/ WEIGHTED_AREA（面的保面积算法）/
            BEND_SIMPLIFY（只用于线）/ EFFECTIVE_AREA。
        用途：数据太大时抽稀能显著减小体积；打印/出图时按比例抽稀。
        """
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params={"algorithm": algorithm, "tolerance": float(tolerance)},
                                   overwrite=overwrite)


class Densify(GeoProcessTool):
    """加密顶点。"""

    name = "densify"
    operation = "densify"

    def densify(self, ctx: Context, in_features: str, out_features: str,
                method: str = "DISTANCE", distance: Optional[float] = None,
                max_deviation: Optional[float] = None, max_angle: Optional[float] = None,
                overwrite: bool = False) -> str:
        """在顶点之间插入更多顶点（加密）。

        method: DISTANCE（默认，按最大间距加密，需给 distance）/
            ANGLE（按最大角度加密，需给 max_angle，单位度）/
            MAX_DEVIATION（按最大偏移加密，需给 max_deviation）。
        用途：数据太稀疏导致平滑/裁剪效果不好时先加密；也常用于让几何更贴合实际形状。
        """
        params: Dict[str, Any] = {"densification_method": method}
        if distance is not None:
            params["distance"] = float(distance)
        if max_deviation is not None:
            params["max_deviation"] = float(max_deviation)
        if max_angle is not None:
            params["max_angle"] = float(max_angle)
        return self.run_geoprocess(ctx, in_features=[in_features], out_features=out_features,
                                   params=params, overwrite=overwrite)
