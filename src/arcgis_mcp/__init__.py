"""arcgis-mcp：基于 arcpy 的 MCP 工具服务。

模块划分：
- config    配置加载（数据源别名 -> 真实路径）
- resolver  把 AI 填的“别名/短名”解析成磁盘完整路径
- arcpy_worker  在 ArcGIS Pro 自带 Python(3.9 + arcpy) 下运行的常驻工作进程
- worker_client 管理 worker 子进程并做 JSON 行协议调用
- server    MCP 服务端（HTTP / stdio），对外暴露工具
"""

__version__ = "0.1.0"
