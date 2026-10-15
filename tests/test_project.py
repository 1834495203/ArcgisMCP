"""项目作用域（project.py）单元测试，不需要 arcpy。

覆盖：自动扫描项目目录、别名冲突、分层合并优先级、相对路径解析、
use_project 会话切换、识别不到项目时的行为。

运行：
    .venv\\Scripts\\python.exe tests\\test_project.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arcgis_mcp.core.config import Settings, Workspace, load_project_config, load_settings  # noqa: E402
from arcgis_mcp.core.project import ProjectRegistry, discover_sources  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, got, expected) -> None:
    global PASSED, FAILED
    if got == expected:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}\n       期望: {expected}\n       实际: {got}")


def make_project(base: Path, name: str) -> Path:
    """造一个假项目：一个 gdb 目录、一个含 .shp 的目录、一个噪音目录。"""
    root = base / name
    (root / "data" / "Project.gdb").mkdir(parents=True)
    (root / "data" / "shp").mkdir(parents=True)
    (root / "data" / "shp" / "roads.shp").write_bytes(b"")
    (root / ".venv" / "dist" / "Noise.gdb").mkdir(parents=True)
    (root / "node_modules").mkdir(parents=True)
    (root / "readme.md").write_text("x", encoding="utf-8")
    return root


def aliases_of(view) -> dict:
    """别名统一小写比较（服务端就是这么规范化的）。"""
    return {w.alias.lower(): w for w in view.sources}


def has_alias(view, name: str) -> bool:
    return name.lower() in aliases_of(view)


def base_settings(**overrides) -> Settings:
    settings = load_settings()
    settings.workspaces = {}
    settings.search_roots = []
    settings.forced_project = None
    settings.default_project = None
    settings.use_server_cwd_as_project = False
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="arcgis-mcp-proj-"))
    try:
        print("自动扫描项目目录：")
        alpha = make_project(tmp, "alpha")
        found = discover_sources(alpha, base_settings())
        by_alias = {w.alias: w for w in found}
        check("发现 gdb（别名取自库名，去掉 .gdb）", "Project" in by_alias, True)
        check("gdb 类型正确", by_alias.get("Project").type if "Project" in by_alias else None, "gdb")
        check("发现含 shp 的目录", "shp" in by_alias, True)
        check("噪音目录 .venv 被跳过", any("Noise" in a for a in by_alias), False)
        check("node_modules 被跳过", any("node_modules" in a for a in by_alias), False)
        check("项目根自身不当 shp 目录", "." in by_alias or "" in by_alias, False)

        print("\n识别不到项目目录时的行为：")
        reg = ProjectRegistry(base_settings(), mode="http")
        view = reg.view()
        check("http 模式不会拿服务 cwd 当项目", view.root, None)
        check("此时 source 为 none", view.source, "none")
        check("data 目录没被误当成搜索根", view.settings.search_roots, [])

        print("\n请求头指定项目（HTTP 客户端的方式）：")
        view = reg.view(header=str(alpha))
        check("项目根 = 请求头里的目录", view.root, alpha)
        check("来源标记为 header", view.source, "header")
        check("项目目录进入搜索根", view.settings.search_roots[0], alpha)
        check("扫到的源都标记为 auto", {w.origin for w in view.sources}, {"auto"})

        print("\nuse_project 会话切换：")
        beta = make_project(tmp, "beta")
        (beta / "data" / "Extra.gdb").mkdir(parents=True)
        reg.set_session_project("sess-1", str(beta))
        view_beta = reg.view(session_key="sess-1")
        check("切到 beta 后项目根变了", view_beta.root, beta)
        check("来源标记为 session", view_beta.source, "session")
        check("beta 里能看到 Extra.gdb", has_alias(view_beta, "extra"), True)
        view_other = reg.view(session_key="sess-2")
        check("别的会话不受影响", view_other.root, None)

        print("\n会话 < 请求头 的优先级：")
        view_hdr = reg.view(header=str(alpha), session_key="sess-1")
        check("同时给会话和请求头时，会话（显式 use_project）赢", view_hdr.root, beta)

        print("\n项目配置 + 分层合并：")
        cfg = beta / ".arcgis-mcp" / "datasources.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "workspaces": {
                "mydata": {"type": "gdb", "path": "data\\Extra.gdb", "description": "相对路径"},
                "shared": {"type": "shp", "path": "data\\shp"},
            }
        }), encoding="utf-8")
        settings = base_settings()
        settings.workspaces = {"global_only": Workspace(alias="global_only", type="gdb",
                                                        path=tmp / "nowhere.gdb", origin="global")}
        reg2 = ProjectRegistry(settings, mode="http")
        reg2.set_session_project("s", str(beta))
        view2 = reg2.view(session_key="s")
        aliases = {w.alias: w for w in view2.sources}
        check("项目配置里的别名生效", "mydata" in aliases, True)
        check("相对路径按项目目录解析", aliases["mydata"].path, beta / "data" / "Extra.gdb")
        check("项目配置的 origin=project", aliases["mydata"].origin, "project")
        check("全局共享数据源也在", "global_only" in aliases, True)
        check("全局的 origin=global", aliases["global_only"].origin, "global")
        check("未被配置覆盖的路径仍会被自动发现",
              aliases.get("project") is not None and aliases["project"].origin == "auto", True)
        check("同一路径不会重复列出（Extra.gdb 只以 mydata 出现，不再有 extra）",
              "extra" not in aliases, True)
        listed_paths = [str(w.path) for w in view2.sources]
        check("sources 里没有重复路径", len(listed_paths), len(set(listed_paths)))

        print("\n同名别名：项目层赢，全局层被记录为 shadowed：")
        settings3 = base_settings()
        settings3.workspaces = {"mydata": Workspace(alias="mydata", type="gdb",
                                                    path=tmp / "old.gdb", origin="global")}
        reg3 = ProjectRegistry(settings3, mode="http")
        reg3.set_session_project("s", str(beta))
        view3 = reg3.view(session_key="s")
        chosen = {w.alias: w for w in view3.sources}["mydata"]
        check("用的是项目里的路径", chosen.path, beta / "data" / "Extra.gdb")
        check("被遮蔽的别名有记录", len(view3.shadowed), 1)

        print("\n别名冲突时自动发现退回相对路径别名：")
        gamma = make_project(tmp, "gamma")
        (gamma / "sub" / "Project.gdb").mkdir(parents=True)
        found_g = discover_sources(gamma, base_settings())
        reg4 = ProjectRegistry(base_settings(), mode="http")
        reg4.set_session_project("s", str(gamma))
        view4 = reg4.view(session_key="s")
        names = [w.alias for w in view4.sources]
        check("同名的两个 Project.gdb 都可用且别名唯一", len(names), len(set(n.lower() for n in names)))

        print("\n项目配置写坏时不崩：")
        bad = make_project(tmp, "broken")
        (bad / ".arcgis-mcp").mkdir(parents=True)
        (bad / ".arcgis-mcp" / "datasources.json").write_text("{ this is not json", encoding="utf-8")
        reg5 = ProjectRegistry(base_settings(), mode="http")
        reg5.set_session_project("s", str(bad))
        view5 = reg5.view(session_key="s")
        check("仍然能返回视图", view5.root, bad)
        check("问题被记录下来", any("无法解析" in p for p in view5.problems), True)
        check("自动发现照常工作", has_alias(view5, "project"), True)

        print("\nstdio 模式用 cwd 当项目目录：")
        import os
        old_cwd = Path.cwd()
        os.chdir(alpha)
        try:
            view_stdio = ProjectRegistry(base_settings(), mode="stdio").view()
            check("stdio 取 cwd", view_stdio.root, alpha)
            check("来源标记为 cwd", view_stdio.source, "cwd")
        finally:
            os.chdir(old_cwd)

        print("\n--project 强制指定优先级最高：")
        reg6 = ProjectRegistry(base_settings(forced_project=alpha), mode="http")
        view6 = reg6.view(header=str(beta), session_key="s")
        check("cli 强制覆盖请求头", view6.root, alpha)
        check("来源标记为 cli", view6.source, "cli")

        print()
        if FAILED:
            print(f"共 {FAILED} 项失败，{PASSED} 项通过")
        else:
            print(f"全部 {PASSED} 项通过 ✅")
        return 1 if FAILED else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
