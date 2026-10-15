"""工具族的中间基类。

`Tool`（在 core/tool.py）是所有工具的抽象基类，负责日志、项目门禁、arcpy 调用等；
这里再加上两种「一族工具共用同一套流程」的基类，让每个工具类只剩参数声明 + 说明：

- `GeoProcessTool`：**产出新数据集**的操作（in → out）。子类只需声明 `operation`
  （对应 core/ops 里的操作名），然后在方法里调 `self.run_geoprocess(...)`。
  统一处理：输入输出路径解析、输出容器自动创建、覆盖保护（默认不覆盖）、
  输出数据集摘要（要素数 / 几何类型 / 坐标系 / 字段）。
- `EditTool`：复制后编辑并保留新版本（字段编辑、修复几何、回收副本）。
  子类声明 `operation`，方法里调 `self.run_edit(...)`。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional, Sequence, Union

from ..core.mcp_compat import Context
from ..core.resolver import ResolutionError, resolve_input_path, resolve_input_paths, resolve_output_path
from ..core.tool import Tool
from ..core.worker import WorkerError


class GeoProcessTool(Tool):
    """产出新数据集的分析工具基类。"""

    needs_project = True
    #: 对应 core/ops 里 GEOPROCESSING 的操作名
    operation: ClassVar[str] = ""

    def run_geoprocess(
        self,
        ctx: Context,
        *,
        in_features: Union[str, Sequence[str], None] = None,
        out_features: str,
        params: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
        extra_inputs: Optional[Sequence[str]] = None,
    ) -> str:
        view = self.view(ctx)
        settings = view.settings
        try:
            inputs: list = []
            if in_features:
                inputs.extend(resolve_input_paths(in_features, settings, "输入数据"))
            for item in (extra_inputs or []):
                inputs.append(resolve_input_path(item, settings, "输入数据"))
            output = resolve_output_path(out_features, settings)
        except ResolutionError as exc:
            return self.error("ResolutionError", exc, view,
                              operation=self.operation, out_features=out_features)

        self.trace(f"{self.operation}：{len(inputs)} 个输入 -> {output}", ctx)
        try:
            result = self.arcpy(
                "geoprocess", ctx=ctx,
                operation=self.operation,
                inputs=[str(p) for p in inputs],
                output=str(output),
                params=params or {},
                overwrite=bool(overwrite),
            )
        except WorkerError as exc:
            return self.error("ArcPyError", exc, view, operation=self.operation,
                              out_features=str(output))
        if result.get('pending') or result.get('ok') is False:
            return self.dumps({**self.project_block(view), **result})

        # 用操作返回的真实路径（有些操作会调整后缀，例如 GeoJSON 强制 .geojson）
        actual_output = str(result.get("output") or output)
        payload: Dict[str, Any] = {
            "ok": True,
            "out_features": actual_output,
            "usage_hint": ("要看结果就再调 read_shp_data / read_gdb_data / describe_data 读它，"
                           "或者把它当作下一个工具的输入。"),
            **self.project_block(view),
            **result,
        }
        return self.dumps(payload)


class EditTool(Tool):
    """复制后修改数据集的工具基类，保留输入和输出两个版本。"""

    needs_project = True
    #: 对应 core/ops 里 EDITING 的操作名
    operation: ClassVar[str] = ""

    def run_edit(self, ctx: Context, *, in_features: str,
                 params: Optional[Dict[str, Any]] = None,
                 label: str = "要修改的数据集",
                 extra_inputs: Optional[Sequence[str]] = None) -> str:
        view = self.view(ctx)
        try:
            target = resolve_input_path(in_features, view.settings, label)
            extras = [str(resolve_input_path(p, view.settings, "输入数据"))
                      for p in (extra_inputs or [])]
        except ResolutionError as exc:
            return self.error("ResolutionError", exc, view, in_features=in_features)
        params = dict(params or {})
        if extras:
            params["in_features"] = extras

        self.trace(f"{self.operation}：{target}", ctx)
        try:
            result = self.arcpy("edit", ctx=ctx, operation=self.operation,
                                target=str(target), params=params or {})
        except WorkerError as exc:
            return self.error("ArcPyError", exc, view, in_features=str(target))
        if result.get('pending') or result.get('ok') is False:
            return self.dumps({**self.project_block(view), **result})

        payload: Dict[str, Any] = {
            "ok": True,
            "in_features": str(target),
            'out_features': result['output'],
            "usage_hint": "输入原件保留，修改已在新版本完成；后续步骤请使用 output / out_features 路径。",
            **self.project_block(view),
            **result,
        }
        return self.dumps(payload)
