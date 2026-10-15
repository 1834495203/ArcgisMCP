"""shapefile 相关的工具：list_shp_datasets / read_shp_data。

「需要项目根目录」这个前置条件由基类按 needs_project 自动加进工具说明，这里不用写。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..core.mcp_compat import Context
from ..core.resolver import (
    ResolutionError,
    list_shp_files,
    resolve_shp_file,
    resolve_shp_folder,
)
from ..core.tool import Tool
from ..core.worker import WorkerError


class ListShpDatasets(Tool):
    """列出目录下的 shapefile。"""

    name = "list_shp_datasets"
    needs_project = True

    def list_shp_datasets(self, ctx: Context, folder: str, recursive: bool = False) -> str:
        """列出某个目录下的 shapefile。

        参数 folder：填以 ./ 开头的相对路径（相对项目根目录），例如 "./data/shp"；
            只写目录名 "shp" 也行，别名也可以（见 list_datasources 的 sources[].alias）。
        参数 recursive：是否递归子目录，默认 false。

        拿到名字后这样读数据：read_shp_data(shp="./data/shp/roads.shp")。
        """
        view = self.view(ctx)
        try:
            base = resolve_shp_folder(folder, view.settings)
            files = list_shp_files(folder, view.settings, recursive=recursive)
        except ResolutionError as exc:
            return self.error("ResolutionError", exc, view, folder=folder)

        items = [
            {"name": p.stem, "relative_path": str(p.relative_to(base)).replace(os.sep, "/"),
             "path": str(p)}
            for p in files if not p.name.lower().startswith(('mcpwork_', 'mcpfailed_', 'mcptrash_'))
        ]
        example = items[0]["relative_path"] if items else "<name>"
        payload: Dict[str, Any] = {
            "ok": True, "folder": folder, "resolved_path": str(base),
            **self.project_block(view),
            "count": len(items), "items": items,
            "usage": f'read_shp_data(shp="{example}")',
        }
        if len(items) > 300:
            payload["items"] = items[:300]
            payload["truncated"] = True
        return self.dumps(payload)


class ReadShpData(Tool):
    """读 shapefile。"""

    name = "read_shp_data"
    needs_project = True

    def read_shp_data(
        self,
        ctx: Context,
        shp: str,
        folder: Optional[str] = None,
        where: Optional[str] = None,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        geometry: str = "none",
        order_by: Optional[str] = None,
        schema_detail: bool = False,
    ) -> str:
        """读取 shapefile 的数据，返回表结构 + 属性记录。

        参数
        ----
        shp: shapefile 的位置。**推荐写以 ./ 开头的相对路径**，相对的是你先用
             use_project 指定的项目根目录，例如 "./shp/roads.shp"（带不带 .shp 都行）。
             只写名字也可以（服务端会在项目根目录下按名字找），例如 "roads"。
        folder: 可选。如果项目里有多个同名 shapefile，用它指定所在的相对目录，
             例如 "./shp"。
        where: 可选 SQL 筛选，例："LANES > 4"、"NAME = 'G6'"。
             字段名用真实名（shapefile 里会被截断到 10 个字符）。
        fields: 可选，只取这些字段，例如 ["NAME", "LANES"]。
        limit: 返回条数上限，默认取服务端配置；limit=0 只返回表结构与计数。
        offset: 跳过前 N 条，配合 limit 翻页。
        geometry: "none"（默认）/ "wkt" / "xy"。
        schema_detail: 默认 false，仅返回字段 name/type；true 返回长度、精度等完整字段结构。
        order_by: 可选排序，例如 "LANES DESC"。

        返回
        ----
        与 read_gdb_data 一致：path、fields、total_count、matched_count、returned、
        truncated、next_offset、records，另有 shape_type 与坐标系。

        示例
        ----
        read_shp_data(shp="./shp/roads.shp", limit=10)
        read_shp_data(shp="roads", where="LANES > 4", limit=10)
        read_shp_data(shp="./矢量/poi.shp", fields=["NAME"], limit=5)
        """
        view = self.view(ctx)
        settings = view.settings

        try:
            path = resolve_shp_file(shp, folder, settings)
        except ResolutionError as exc:
            return self.error("ResolutionError", exc, view, shp=shp, folder=folder)

        effective_limit = self.resolve_limit(limit, view)
        clamped = limit is not None and int(limit) > settings.max_limit
        self.trace(f"读取 {path}（limit={effective_limit}, geometry={geometry}）", ctx)
        try:
            result = self.arcpy(
                "read", ctx=ctx,
                workspace=str(path),
                dataset=None,
                where=where or None,
                fields=list(fields) if fields else None,
                limit=effective_limit,
                offset=int(offset or 0),
                geometry=geometry or "none",
                order_by=order_by or None,
            )
        except WorkerError as exc:
            return self.error("ArcPyError", exc, view, shp=shp, folder=folder,
                              resolved_path=str(path),
                              hint="可用 list_shp_datasets 确认 shapefile 名字。")

        payload: Dict[str, Any] = {"ok": True, "shp": shp, "folder": folder, **result}
        if clamped:
            payload["limit_clamped"] = (
                f"limit 被限制为 {effective_limit}（服务端上限 {settings.max_limit}）"
            )
        self.pagination_hint(payload, offset, result)
        return self.dumps(self.shrink(self.compact_schema(payload, schema_detail), view))
