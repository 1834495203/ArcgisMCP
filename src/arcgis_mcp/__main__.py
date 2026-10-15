"""命令行入口。

HTTP 模式（一个服务实例给多个项目共用）：
    python -m arcgis_mcp --transport http --host 127.0.0.1 --port 8765
    客户端连 http://127.0.0.1:8765/mcp，AI 首次调用 use_project 告诉服务端项目目录

stdio 模式（每个项目一个进程，项目目录由客户端工作目录自动识别，零配置）：
    python -m arcgis_mcp --transport stdio

给某个项目固定开一个实例：
    python -m arcgis_mcp --transport http --project D:\\projects\\myproj

把项目里的数据登记成显式配置（可选，自动扫描已经够用）：
    python -m arcgis_mcp --init-project D:\\projects\\myproj

自检（不启动服务）：
    python -m arcgis_mcp --check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .core.config import ConfigError, expand_path, load_settings
from .core.logs import get_logger, setup

_log = get_logger("cli")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="arcgis-mcp",
        description="基于 arcpy 的 MCP 工具服务（gdb / shapefile 数据读取，数据源跟随项目目录）",
    )
    parser.add_argument("--version", action="version", version=f"arcgis-mcp {__version__}")
    parser.add_argument(
        "--transport",
        choices=["http", "streamable-http", "stdio"],
        default=os.environ.get("ARCGIS_MCP_TRANSPORT", "http"),
        help="传输方式：http(等价 streamable-http，默认) 或 stdio",
    )
    parser.add_argument("--host", default=os.environ.get("ARCGIS_MCP_HOST", "127.0.0.1"),
                        help="HTTP 监听地址，默认 127.0.0.1（只本机可访问）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARCGIS_MCP_PORT", "8765")),
                        help="HTTP 监听端口，默认 8765")
    parser.add_argument("--path", default=os.environ.get("ARCGIS_MCP_HTTP_PATH", "/mcp"),
                        help="HTTP 路径，默认 /mcp")
    parser.add_argument("--config", default=os.environ.get("ARCGIS_MCP_CONFIG"),
                        help="全局配置文件路径，默认 <项目>/config/datasources.json")
    parser.add_argument("--project", default=None,
                        help="强制指定项目目录（覆盖请求头/环境变量/cwd 的识别结果）")
    parser.add_argument("--check", action="store_true",
                        help="只做自检：打印配置、启动 arcpy 工作进程并 ping，然后退出")
    parser.add_argument("--init-project", nargs="?", const=".", default=None, metavar="DIR",
                        help="扫描 DIR（默认当前目录）并生成 <DIR>/.arcgis-mcp/datasources.json，"
                             "同时打印各客户端的接入配置片段")
    parser.add_argument("--force", action="store_true", help="配合 --init-project：覆盖已存在的配置")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def _check(args: argparse.Namespace) -> int:
    from . import server as server_module

    settings = load_settings(args.config)
    _apply_project_override(args, settings)
    mode = "stdio" if args.transport == "stdio" else "http"
    app = server_module.build_app(settings, mode)

    try:
        snapshot = server_module.diagnostics(app)
        print(json.dumps(snapshot["settings"], ensure_ascii=False, indent=2))
        print("\n当前项目作用域：")
        print(json.dumps(snapshot["project"], ensure_ascii=False, indent=2))
        print(f"\n已注册工具：{'、'.join(snapshot['tools'])}")
        if snapshot["problems"]:
            print("\n配置警告：", file=sys.stderr)
            for problem in snapshot["problems"]:
                print(f"  - {problem}", file=sys.stderr)

        _log.info("启动 arcpy 工作进程做 ping 自检（首次需要 import arcpy）...")
        try:
            ping = app.worker.call("ping")
        except Exception as exc:
            print(f"\narcpy 自检失败：{exc}", file=sys.stderr)
            return 1
        print("\narcpy 环境：")
        print(json.dumps(ping, ensure_ascii=False, indent=2))

        if app.projects.view().root is None:
            print("\n注意：没有识别到项目目录。HTTP 模式下请让客户端带请求头 X-Project-Root、"
                  "或让 AI 调用 use_project、或启动时加 --project <目录>；"
                  "stdio 模式下会取客户端的工作目录。⚠️")
        else:
            root = app.projects.view().root
            print(f"\n自检通过 ✅（项目目录：{root}）")
        return 0
    finally:
        app.shutdown()


def _init_project(args: argparse.Namespace, settings) -> int:
    from .core.project import client_snippets, discover_sources

    root = expand_path(args.init_project).resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    target = root / settings.project_config_rel
    if target.exists() and not args.force:
        print(f"配置已存在，未覆盖：{target}\n（要覆盖请加 --force）", file=sys.stderr)

    aliases: dict = {}
    used: set = set()
    for ws in discover_sources(root, settings):
        alias = ws.alias.strip().lower()
        if alias in used:
            rel = str(ws.path.relative_to(root)).replace(os.sep, "/")
            if ws.type == "gdb":
                rel = rel[: -len(ws.path.suffix)]
            alias = rel.lower()
        index = 2
        while alias in used:
            alias = f"{alias}-{index}"
            index += 1
        used.add(alias)
        aliases[alias] = {"type": ws.type, "path": str(ws.path),
                          "description": f"自动扫描发现（{ws.path.name}）"}

    if not (target.exists() and not args.force):
        target.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "_readme": [
                "arcgis-mcp 项目级数据源配置（由 --init-project 生成，可自由修改）。",
                "这些别名只对当前项目生效；不写别名也能按名字读取，因为项目目录本身就是搜索根。",
                "type: gdb = 地理数据库，shp = 存放 shapefile 的目录。",
            ],
            "workspaces": aliases,
        }
        target.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入 {target}（{len(aliases)} 个数据源）")

    if not aliases:
        print(f"注意：在 {root} 下没有发现 .gdb 或含 shapefile 的目录。"
              f"数据在别处的话，可以直接把它们加进 {target}。", file=sys.stderr)

    print(f"\n项目目录：{root}")
    print("\n让客户端把项目目录告诉服务端的配置片段：")
    for name, snippet in client_snippets(str(root)).items():
        print(f"\n--- {name} ---\n{snippet}")
    return 0


def _apply_project_override(args: argparse.Namespace, settings) -> None:
    if args.project:
        settings.forced_project = expand_path(args.project).resolve()
        _log.info("已强制指定项目目录：%s", settings.forced_project)


# --------------------------------------------------------------------------
# 启动服务
# --------------------------------------------------------------------------
def _serve(args: argparse.Namespace, settings) -> int:
    from . import server as server_module

    transport = "streamable-http" if args.transport == "http" else args.transport
    mode = "stdio" if transport == "stdio" else "http"
    app = server_module.build_app(settings, mode)
    server = server_module.build_server(app)

    for problem in server_module.config_problems(app):
        _log.warning("配置警告：%s", problem)

    try:
        if transport == "streamable-http":
            # mcp 1.x 从 settings 读 host/port，2.x 支持直接传参，两种都设置一遍最稳
            os.environ.setdefault("FASTMCP_HOST", args.host)
            os.environ.setdefault("FASTMCP_PORT", str(args.port))
            _log.info("MCP 服务已启动（HTTP）: http://%s:%s%s", args.host, args.port, args.path)
            _log.info("全局配置：%s", settings.config_path)
            if settings.forced_project:
                _log.info("项目目录（--project 强制）：%s", settings.forced_project)
            else:
                _log.info("项目目录由客户端提供：AI 调 use_project，"
                          "或客户端请求头 X-Project-Root；未提供时需读数据的工具会被拒绝")
            if os.environ.get("ARCGIS_MCP_NO_WARMUP") != "1":
                server_module.warm_up(app)
            server.run(transport="streamable-http", host=args.host, port=args.port,
                       streamable_http_path=args.path)
        else:
            _log.info("MCP 服务已启动（stdio），当前工作目录 %s 将作为项目目录", Path.cwd())
            server.run(transport="stdio")
    finally:
        app.shutdown()
    return 0


def main(argv=None) -> int:
    setup()
    args = parse_args(argv)
    if args.check:
        return _check(args)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        _log.error("配置错误：%s", exc)
        return 2
    _apply_project_override(args, settings)

    if args.init_project is not None:
        return _init_project(args, settings)
    return _serve(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
