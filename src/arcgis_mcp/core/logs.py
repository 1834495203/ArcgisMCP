"""统一日志。

**文件名不能叫 logging.py**：worker 是以脚本目录（core/）为 sys.path[0] 启动的，
叫 logging 会遮蔽标准库的 logging —— arcpy 内部脚本 / PIL 一 import logging
就会拿到这个文件，报「module 'logging' has no attribute 'getLogger'」（踩过）。
同理，core/ 下不要放名字与标准库模块冲突的文件。

服务端的所有输出都走这里，方便统一控制格式和级别：
- 写到 stderr（stdout 在 stdio 模式下是 MCP 协议通道，绝不能污染）；
- 需要让 AI 看到的进度信息，用 Tool.trace() 推给 MCP 客户端（见 core/tool.py）。

环境变量：
    ARCGIS_MCP_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR   默认 INFO
"""

from __future__ import annotations

import logging
import os
import sys

ROOT_LOGGER = "arcgis_mcp"
_configured = False

DEFAULT_FORMAT = "[arcgis-mcp] %(asctime)s %(levelname)-7s %(name)s: %(message)s"
SIMPLE_FORMAT = "[arcgis-mcp] %(message)s"


def setup(level: str | int | None = None, fmt: str | None = None) -> logging.Logger:
    """初始化日志（幂等，重复调用不会重复加 handler）。"""
    global _configured
    logger = logging.getLogger(ROOT_LOGGER)
    if _configured:
        return logger

    # 控制台可能是 GBK 等窄编码，中文日志有编码失败的风险：
    # 只把错误处理改成 replace，保证任何情况下都不会因为打日志而抛异常。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    if level is None:
        level = os.environ.get("ARCGIS_MCP_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.strip().upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt or DEFAULT_FORMAT, datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """取一个子 logger，例如 get_logger("tools.read_gdb_data")。"""
    setup()
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def info(message: str) -> None:
    """给不方便拿 logger 的地方用的快捷方法（保持 [arcgis-mcp] 前缀风格）。"""
    get_logger("").info(message)


def brief(value, limit: int = 160) -> str:
    """把入参/结果压成一行，避免日志刷屏。"""
    import json

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"
