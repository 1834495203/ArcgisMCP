"""组装：把配置、项目作用域、arcpy 工作进程、工具、中间件拼成一个 MCP 服务。

这里只做组装，具体实现都在：
- core/   基础设施（配置、项目作用域、路径解析、arcpy 子进程、日志、工具基类）
- tools/  具体工具
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .core.config import Settings, load_settings, self_check
from .core.logs import get_logger, setup
from .core.mcp_compat import MCPServer
from .core.middleware import ToolCallLogger
from .core.project import ProjectRegistry
from .core.tool import Services
from .core.worker import ArcPyWorker
from .tools import register_all, tool_names

INSTRUCTIONS = """\
本服务用本机 arcpy 读取、处理 ArcGIS 数据，并保留整个数据集的加工链路。

用法就是两步：

1. 先把项目根目录告诉服务端：use_project(folder="<项目根目录的绝对路径>")。
   项目根目录通常就是你正在工作的那个目录（在终端里 pwd 一下就知道了）。
   服务端会记住它，并自动把该目录下的 *.gdb 和含 shapefile 的目录找出来。

2. 之后所有工具都用相对路径（以 ./ 开头），服务端自己拼上项目根目录：
     read_gdb_data(gdb="./data/City.gdb", dataset="roads")
     read_shp_data(shp="./shp/roads.shp")
   只写名字也可以，服务端会在项目根目录下按名字找：
     read_gdb_data(gdb="City.gdb", dataset="roads")

不需要记别名，也不需要填磁盘绝对路径。

没设置项目根目录时，需要读数据的工具会被服务端直接拒绝（返回错误并提示先调 use_project），
所以不用试探——看到那个提示就照做。

如果 list_datasources 返回的 project_root 已经有值（客户端通过 stdio 工作目录或请求头
告诉过服务端），那第 1 步可以跳过。

读数据时的习惯：先用 limit=0 看表结构，再小 limit 抽样，需要更多就用 next_offset 翻页。

默认提供 24 个核心接口：项目读取、任务审计、工作流与 ArcPy 目录查询。
GIS 处理通过节点脚本执行，不要尝试调用工具列表中未注册的固定封装或通用执行入口。
先用 configure_workflow_data 固定过程、临时、交付库，不按步骤或实验新建 GDB。
探索和验证用 run_workflow_step(mode="draft")；采用的方法以 final 重算，验证由 AI 编写 step.check。
可复用流程用 save_workflow_node 保存 Python 正文、说明、理由和输入输出，
节点输入通过 @节点ID.输出别名 引用上游；workflow_nodes 查询，run_saved_workflow 运行。
list_workflows 列已有定义；workflow_steps 查实际执行历史；export_workflow 导出脚本，
run_workflow 用于批量脚本或导出流程重跑。修改定义不影响先前提交的代码快照。
输出路径由服务端分配；编辑用 edit_inputs 或 step.edit_copy，在副本上操作，保留上游成果。
输入默认 snapshot 保真复制；显式 read_only 支持直接读取更多数据类型，但仅为只读约定，
强制保护原件需要部署执行身份的只读权限。任意脚本不是 Python/文件系统安全沙箱。
list_arcpy_tools / describe_arcpy_tool 用于查询 ArcPy 语法，execution 是兼容适配器元数据，
不代表对应 MCP 执行接口已注册，也不限制受审计节点内可使用的 ArcPy 算法。
tool_profile=legacy 可在服务启动时额外注册旧封装和通用入口；以实际 tools/list 为准。
任务返回 job_id 后先查 job_result；客户端超时不表示操作停止，unknown 不自动重放。
lineage / explain 默认精简分页；通过 next_offset 继续，detail=true 取完整步骤。
read_* / describe_data 默认只返回字段 name/type，schema_detail=true 获取完整字段结构。
job_result(wait_seconds=5) 可短暂等待后台任务，不占 ArcPy 锁；detail=true 获取完整原始结果。
工具成功表示执行及输出发布成功，不代表业务验收通过。面积差异、是否需要继续融合/消除，
由调用方按任务要求自行查询和判断；MCP 不对要素数未减少或 where 零命中额外判错。
"""

_log = get_logger("server")


@dataclass
class App:
    """一次服务运行的全部状态（替代原先的模块级全局变量，方便测试和多实例）。"""

    settings: Settings
    projects: ProjectRegistry
    worker: ArcPyWorker
    mode: str = "http"

    @property
    def services(self) -> Services:
        return Services(settings=self.settings, projects=self.projects,
                        worker=self.worker, mode=self.mode)

    def shutdown(self) -> None:
        self.worker.shutdown()


def build_app(settings: Optional[Settings] = None, mode: str = "http") -> App:
    """加载配置并组装运行环境（此时还不启动 arcpy 子进程）。"""
    setup()
    settings = settings or load_settings()
    _log.info("模式=%s，全局配置=%s", mode, settings.config_path)
    return App(
        settings=settings,
        projects=ProjectRegistry(settings, mode=mode),
        worker=ArcPyWorker(settings),
        mode=mode,
    )


def build_server(app: App) -> MCPServer:
    """建 MCP 服务并注册所有工具与中间件。"""
    server = MCPServer(name="arcgis-mcp", instructions=INSTRUCTIONS,
                       middleware=[ToolCallLogger()])
    instances = register_all(server, app.services)
    _log.info("工具配置=%s，已注册 %d 个工具：%s", app.settings.tool_profile, len(instances),
              "、".join(t.name for t in instances))
    return server


def warm_up(app: App) -> None:
    """预热 arcpy（import 要十几到几十秒，先在后台拉起来）。"""
    import threading

    def _run() -> None:
        try:
            info = app.worker.call("ping")
            _log.info("arcpy 预热完成：Python %s / arcpy %s",
                      info.get("python"), info.get("arcpy_version"))
        except Exception as exc:
            _log.warning("arcpy 预热失败（不影响启动，首次调用时会再试）：%s", exc)

    threading.Thread(target=_run, name="arcpy-warmup", daemon=True).start()


def config_problems(app: App) -> List[str]:
    return self_check(app.settings)


def diagnostics(app: App) -> Dict[str, Any]:
    """给 CLI --check 用的一份环境快照。"""
    view = app.projects.view()
    return {
        "settings": app.settings.to_dict(),
        "project": view.to_dict(),
        "worker": app.worker.status(),
        "tools": tool_names(app.settings),
        "problems": self_check(app.settings) + view.problems,
    }
