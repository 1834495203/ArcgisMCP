"""基础设施层（core）。

- config      配置加载与校验
- project     项目作用域：项目根目录的识别、扫描、分层合并
- resolver    名字/相对路径 → 完整路径
- worker      arcpy 子进程的管理（复用、超时、崩溃重启）
- arcpy_worker 真正在 ArcGIS Pro 的 Python 3.9 下跑的 arcpy 操作
- logging     统一日志
- mcp_compat  mcp SDK 版本兼容导入（唯一允许 try/except import 的地方）
- middleware  统一记录每次工具调用
- tool        工具抽象基类（写新工具只用继承它）

这里放的都是「与具体工具无关」的东西；具体工具在 ../tools/。
"""

from .config import (
    ConfigError,
    ProjectConfig,
    Settings,
    Workspace,
    expand_path,
    load_project_config,
    load_settings,
    self_check,
)
from .logs import get_logger, setup
from .mcp_compat import Context, MCPServer, ToolError, request_headers
from .project import (
    PROJECT_HEADER,
    ProjectRegistry,
    ProjectView,
    client_snippets,
    discover_sources,
    encode_header_path,
)
from .resolver import (
    ResolutionError,
    list_shp_files,
    resolve_gdb,
    resolve_shp_file,
    resolve_shp_folder,
    split_gdb_dataset_ref,
    walk_dirs,
)
from .tool import Services, Tool, require_project
from .worker import ArcPyWorker, WorkerError

__all__ = [
    "Settings", "Workspace", "ProjectConfig", "ConfigError", "load_settings",
    "load_project_config", "self_check", "expand_path",
    "ProjectRegistry", "ProjectView", "PROJECT_HEADER", "discover_sources",
    "encode_header_path", "client_snippets",
    "ResolutionError", "resolve_gdb", "resolve_shp_file", "resolve_shp_folder",
    "list_shp_files", "split_gdb_dataset_ref", "walk_dirs",
    "ArcPyWorker", "WorkerError",
    "Tool", "Services", "require_project",
    "Context", "MCPServer", "ToolError", "request_headers",
    "get_logger", "setup",
]
