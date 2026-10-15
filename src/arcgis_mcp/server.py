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
from .tools import ALL_TOOLS, register_all

INSTRUCTIONS = """\
本服务用本机 arcpy 读取 ArcGIS 数据（gdb / shapefile）。

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
    register_all(server, app.services)
    _log.info("已注册 %d 个工具：%s", len(ALL_TOOLS),
              "、".join(t.name for t in ALL_TOOLS))
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
        "tools": [t.name for t in ALL_TOOLS],
        "problems": self_check(app.settings) + view.problems,
    }
