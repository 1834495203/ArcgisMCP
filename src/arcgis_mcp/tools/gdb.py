"""gdb（地理数据库）相关的工具：list_gdb_datasets / read_gdb_data。

「需要项目根目录」这个前置条件由基类按 needs_project 自动加进工具说明，这里不用写。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.mcp_compat import Context
from ..core.resolver import ResolutionError, resolve_gdb, split_gdb_dataset_ref
from ..core.tool import Tool
from ..core.worker import WorkerError


class ListGdbDatasets(Tool):
    """列出 gdb 里的要素类 / 表。"""

    name = "list_gdb_datasets"
    needs_project = True

    def list_gdb_datasets(self, ctx: Context, gdb: str) -> str:
        """列出某个地理数据库（gdb）里所有的要素类 / 表。

        参数 gdb：填以 ./ 开头的相对路径（相对项目根目录），例如 "./data/City.gdb"；
            只写库名 "City.gdb" 也行。别名也可以（见 list_datasources 的 sources[].alias）。
        返回每一项的 name / relative_path / 几何类型 / 坐标系。

        拿到名字后这样读数据：read_gdb_data(gdb="./data/City.gdb", dataset="<name>")。
        注意：要素数据集里的要素类用 "/" 连接，例如 dataset="base/rivers"。
        """
        view = self.view(ctx)
        try:
            path = resolve_gdb(gdb, view.settings)
            result = self.arcpy("list_datasets", ctx=ctx, workspace=str(path))
        except (ResolutionError, WorkerError) as exc:
            return self.error(type(exc).__name__, exc, view, gdb=gdb)

        payload: Dict[str, Any] = {
            "ok": True, "gdb": gdb, "resolved_path": str(path),
            **self.project_block(view), **result,
        }
        if len(self.dumps(payload).encode("utf-8")) > view.settings.max_response_bytes:
            payload["items"] = payload.get("items", [])[:200]
            payload["truncated"] = True
        return self.dumps(payload)


class ReadGdbData(Tool):
    """读 gdb 里的要素类 / 表。"""

    name = "read_gdb_data"
    needs_project = True

    def read_gdb_data(
        self,
        ctx: Context,
        gdb: str,
        dataset: Optional[str] = None,
        where: Optional[str] = None,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        geometry: str = "none",
        order_by: Optional[str] = None,
        schema_detail: bool = False,
    ) -> str:
        """读取地理数据库（gdb）里某个要素类或表的数据，返回表结构 + 属性记录。

        参数
        ----
        gdb: 地理数据库的位置。**推荐写以 ./ 开头的相对路径**，相对的是你先用
             use_project 指定的项目根目录，例如 "./data/City.gdb"。
             只写名字也可以（服务端会在项目根目录下按名字找），例如 "City.gdb"。
             也可以把要素类一起带上，那样 dataset 就不用填了：
             "./data/City.gdb/cities"。
        dataset: 要读的要素类 / 表名，例如 "cities"。
             要素数据集里的要素类写成 "featuredataset/featureclass"，例如 "base/rivers"。
             **不填的话服务端会把库里有哪些要素类/表列出来**，你挑一个再读。
        where: 可选 SQL 筛选，字段名用真实名字（不是别名），字符串用单引号。
             例："POP > 1000000 AND NAME LIKE '北%'"。
        fields: 可选，只取这些字段，格式 ["NAME", "POP"]。
             几何字段不在这里指定，用 geometry 参数控制。
        limit: 返回记录条数上限，默认取服务端配置（一般 100）。
             **limit=0 只返回表结构 + 计数，不返回记录**，适合先看 schema。
        offset: 跳过前 N 条记录，配合 limit 分页。
        geometry: "none"（默认，最快）/ "wkt"（WKT 文本）/ "xy"（坐标值）。
        schema_detail: 默认 false，仅返回字段 name/type；true 返回长度、精度等完整字段结构。
        order_by: 可选排序，例如 "POP DESC"。

        返回
        ----
        JSON 字符串：path（实际读的完整路径，仅供核对）、fields（字段结构）、
        total_count、matched_count、returned、truncated 与 next_offset（翻页用）、
        shape_type、spatial_reference、extent、records。

        示例
        ----
        read_gdb_data(gdb="./data/City.gdb", dataset="cities", limit=5)
        read_gdb_data(gdb="./data/City.gdb/cities", limit=5)          # 要素类写在路径里
        read_gdb_data(gdb="./data/City.gdb")                          # 先看看有什么
        read_gdb_data(gdb="./data/City.gdb", dataset="cities",
                      where="POP > 1000000", fields=["NAME", "POP"])
        """
        view = self.view(ctx)
        settings = view.settings

        workspace_ref, dataset_name = split_gdb_dataset_ref(gdb, dataset or "", settings)
        try:
            path = resolve_gdb(workspace_ref, settings)
        except ResolutionError as exc:
            return self.error("ResolutionError", exc, view, gdb=gdb, dataset=dataset)

        # 没指定要素类：把库里有什么列出来，让 AI 挑一个再读（省一轮试错）
        if not dataset_name:
            return self._catalog(ctx, view, gdb, path)

        effective_limit = self.resolve_limit(limit, view)
        clamped = limit is not None and int(limit) > settings.max_limit
        self.trace(f"读取 {path} / {dataset_name}（limit={effective_limit}, geometry={geometry}）", ctx)
        try:
            result = self.arcpy(
                "read", ctx=ctx,
                workspace=str(path),
                dataset=dataset_name,
                where=where or None,
                fields=list(fields) if fields else None,
                limit=effective_limit,
                offset=int(offset or 0),
                geometry=geometry or "none",
                order_by=order_by or None,
            )
        except WorkerError as exc:
            return self.error("ArcPyError", exc, view, gdb=gdb, dataset=dataset,
                              resolved_gdb=str(path),
                              hint="可用 list_gdb_datasets 确认要素类名字。")

        payload: Dict[str, Any] = {
            "ok": True, "gdb": gdb, "dataset": dataset,
            "workspace_path": str(path), **result,
        }
        if clamped:
            payload["limit_clamped"] = (
                f"limit 被限制为 {effective_limit}（服务端上限 {settings.max_limit}）"
            )
        self.pagination_hint(payload, offset, result)
        return self.dumps(self.shrink(self.compact_schema(payload, schema_detail), view))

    # ------------------------------------------------------------------
    def _catalog(self, ctx: Context, view, gdb: str, path) -> str:
        """只给了 gdb、没给 dataset 时，返回库里有哪些数据集。"""
        try:
            listing = self.arcpy("list_datasets", ctx=ctx, workspace=str(path))
        except WorkerError as exc:
            return self.error("ArcPyError", exc, view, gdb=gdb, resolved_gdb=str(path))
        items = listing.get('items', [])
        return self.dumps({
            "ok": True, "need_dataset": True, "gdb": gdb, "resolved_gdb": str(path),
            **self.project_block(view),
            "datasets": [{"name": i["relative_path"], "data_type": i.get("data_type"),
                          "shape_type": i.get("shape_type")} for i in items],
            "hint": ("还没指定要读哪个要素类/表。请从上面的 datasets 里挑一个填进 dataset "
                     "再调用，例如 read_gdb_data(gdb=\"./data/City.gdb\", dataset=\"cities\")；"
                     "也可以直接写成 read_gdb_data(gdb=\"./data/City.gdb/cities\")。"),
        })
