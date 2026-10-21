"""项目作用域与自检相关的工具：use_project / list_datasources / arcgis_status。

这三类工具**不设**「必须先确定项目根目录」的门禁 —— 它们就是用来告诉服务端
项目目录在哪、以及排查为什么干不了活的。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.config import self_check
from ..core.mcp_compat import Context, ToolError
from ..core.tool import Tool


class UseProject(Tool):
    """把项目根目录告诉服务端（通常第一个该调用的工具）。"""

    name = "use_project"

    def use_project(self, ctx: Context, folder: str, output_gdb: Optional[str] = None,
                    auto_create_gdb: Optional[bool] = None) -> str:
        """【通常第一个该调用的工具】把项目根目录告诉服务端。

        参数 folder：项目根目录的**绝对路径**，例如 "D:\\\\projects\\\\myproject"。
            一般就是你当前工作的那个目录，在终端里 pwd 一下就能拿到。

        调用之后，其他所有工具的路径参数都可以写成以 ./ 开头的相对路径，
        服务端会自动拼上这个项目根目录，例如：

            use_project(folder="D:\\\\projects\\\\myproject")
            read_gdb_data(gdb="./data/City.gdb", dataset="roads")
            read_shp_data(shp="./shp/roads.shp")

        服务端会记住它（按客户端会话记住），所以一个会话里调一次就够了。
        切换项目就再调一次本工具。
        分步脚本流程先用 configure_workflow_data 一次配置原始/过程/临时/交付角色，
        再用 run_workflow_step 交替实验和正式执行；不要为每步或实验创建新 GDB。

        调用时服务端会顺手扫描这个目录，把 *.gdb 和含 shapefile 的目录列出来（见返回值 sources）。
        output_gdb: 可选，当前会话的默认输出 GDB，例如 ./工作.gdb；只选择，不在这里建库。
            产出工具的裸名称如 D_smooth 会写入该库，显式路径不变；编辑仍在源所在库产生副本。
        auto_create_gdb: 可选，false 禁止写输出时自动建缺失的库；此时须预先准备所需库。
            独立 create_gdb 仅 legacy 配置注册，默认通过工作流角色配置按需建库。
            这两个选择仅当前会话生效；持久化可配置项目 .arcgis-mcp/datasources.json。
        """
        view = self.switch_project(ctx, folder)
        if output_gdb is not None or auto_create_gdb is not None:
            try:
                self.services.projects.set_output_policy(self.session_key(ctx), view.root,
                                                          output_gdb, auto_create_gdb)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            view = self.view(ctx)
        self.trace(f"项目根目录已设为 {view.root}，扫到 {len(view.sources)} 个数据源", ctx)

        payload: Dict[str, Any] = {
            "ok": True,
            "project_root": str(view.root),
            "project_root_source": view.source,
            "message": ("已记住项目根目录。之后 gdb / shp / folder 参数可以直接写 "
                        "./相对路径，例如 gdb=\"./data/City.gdb\"。"),
            "search_roots": [str(p) for p in view.settings.search_roots],
            "output_workspace": str(view.settings.output_workspace) if view.settings.output_workspace else None,
            "auto_create_gdb": view.settings.auto_create_gdb,
            "sources": [w.to_dict() for w in view.sources],
        }
        if view.problems:
            payload["problems"] = view.problems
        if not view.sources:
            payload["hint"] = (
                f"在 {view.root} 下没有发现 .gdb 或含 shapefile 的目录。"
                "如果数据不在这个目录里，换成数据所在的那个目录再调一次。"
            )
        return self.dumps(payload)


class ListDataSources(Tool):
    """看当前项目根目录和扫到的数据源。"""

    name = "list_datasources"

    def list_datasources(self, ctx: Context) -> str:
        """列出当前项目下能用的 ArcGIS 数据源（不启动 arcpy，立即返回）。

        一般不用主动调这个 —— 先调 use_project 指定项目根目录，然后直接用
        "./相对路径" 读数据就行。需要确认「项目目录里到底有什么」时再看它。

        返回内容包括：
        - project_root：服务端当前认定的项目根目录（null = 还不知道，先调 use_project）
        - sources：扫到的 gdb / shapefile 目录清单，每项有 alias、path 和 origin
          （project = 项目配置里写的，auto = 自动扫描发现，global = 全局配置）
        - search_roots：按名字查找时用的目录（项目根目录排在最前）
        """
        view = self.view(ctx)
        payload: Dict[str, Any] = {
            "ok": True,
            "usage": "gdb / folder 填 alias；dataset / shp 填数据名。一般不用填路径。",
            **self.project_block(view),
            "search_roots": [str(p) for p in view.settings.search_roots],
            "sources": [w.to_dict() for w in view.sources],
            "default_limit": view.settings.default_limit,
            "output_workspace": str(view.settings.output_workspace) if view.settings.output_workspace else None,
            "auto_create_gdb": view.settings.auto_create_gdb,
            "max_limit": view.settings.max_limit,
        }
        if view.project_config is not None:
            payload["project_config"] = {
                "path": str(view.project_config),
                "exists": view.project_config.is_file(),
                "note": "可以在这个文件里给项目数据起固定别名（可选，自动扫描已经够用）",
            }
        if view.problems:
            payload["problems"] = view.problems
        if view.root is not None and not view.sources:
            payload["hint"] = (
                f"项目目录 {view.root} 下没有发现 .gdb 或含 shapefile 的目录。"
                "确认数据是否放在别处；如果是跨项目共享的数据，"
                f"可以加进全局配置 {view.settings.config_path} 的 workspaces，"
                "或直接把数据目录路径传给 use_project。"
            )
        if not view.root:
            payload["problems"] = list(payload.get("problems", [])) + [
                f"未识别到项目目录；当前只有全局配置里的 {len(view.sources)} 个数据源。"
            ]
        return self.dumps(payload)


class ArcGisStatus(Tool):
    """arcpy 环境自检。"""

    name = "arcgis_status"

    def arcgis_status(self, ctx: Context) -> str:
        """检查 arcpy 环境是否可用，并显示当前项目作用域。

        首次调用会启动 arcpy 工作进程（import arcpy 通常 10-40 秒），之后秒回。
        其他工具报超时或环境错误时，先调这个排查。
        """
        settings = self.settings
        view = self.view(ctx)
        status: Dict[str, Any] = {
            "ok": True,
            "worker": self.services.worker.status(),
            "config_path": str(settings.config_path),
            **self.project_block(view),
        }
        if view.root:
            from ..core.ops.tabular import scratch_status
            status['scratch'] = scratch_status(view.root)
        problems = self_check(settings)
        if problems:
            status["config_warnings"] = problems
        if (status['worker'].get('current_op') or status['worker'].get('queued_calls')
                or status['worker'].get('queued_jobs')):
            status['ready'] = status['worker']['ready']
            status['busy'] = True
            return self.dumps(status)
        try:
            status["arcpy"] = self.arcpy("ping", ctx=ctx)
            status["ready"] = True
        except Exception as exc:
            status["ok"] = False
            status["ready"] = False
            status["error"] = str(exc)
        return self.dumps(status)
