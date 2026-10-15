# ArcgisMCP

通过本机 ArcPy 向 AI 客户端提供 ArcGIS 数据读取、地理处理和加工链路查询，支持 Streamable HTTP 与 stdio。

> **Beta / AI 编写声明：本项目代码由 AI 编写和迭代，可能存在 Bug、兼容性问题及未覆盖的边界情况。测试通过不代表所有场景可靠；请先在测试数据上验证，保留独立备份，重要成果由人工核验。**

## 部署

### 1. 准备环境与安装

需要 Windows、可用的 ArcGIS Pro / ArcPy 环境及相应工具许可，以及独立的 Python 3.10+（推荐 3.12）。MCP 服务和 ArcPy worker 使用不同解释器；不要向服务虚拟环境安装 ArcPy。

当前主要在 ArcGIS Pro 3.1.5 的环境中实测，其他版本需要验证。ArcGIS Pro 和 Python 安装完成后，在 PowerShell 中执行：

```powershell
git clone https://github.com/1834495203/ArcgisMCP.git
cd ArcgisMCP
./scripts/install.bat
```

安装脚本会创建 `.venv`、安装服务依赖，并登记源码路径；脚本默认使用清华 PyPI 镜像。

### 2. 告诉服务 ArcPy 在哪里

修改仓库内 `config/datasources.json` 的 `arcpy_python`，填写**能够导入 ArcPy 的 `python.exe` 完整路径，不是 arcpy 文件夹**。例如默认环境：

```json
{
  "arcpy_python": "C:/Program Files/ArcGIS/Pro/bin/Python/envs/arcgispro-py3/python.exe"
}
```

上面只展示需要修改的字段，保留配置文件其他设置。使用克隆环境时，填写克隆环境中的 `python.exe`。

不知道环境位置时，可在 ArcGIS Pro 的 Python 窗口运行下面代码，获取当前环境中的解释器候选路径，再确认文件存在：

```python
import sys
from pathlib import Path
print(Path(sys.prefix) / "python.exe")
```

也可从 ArcGIS Pro 的包管理器查看当前环境位置。不要直接把 `sys.executable` 的输出填入配置，它在 Pro 内可能指向 `ArcGISPro.exe`。

不想修改文件时，可用环境变量覆盖（优先于配置文件，仅对当前 PowerShell 及其启动的进程生效）：

```powershell
$env:ARCGIS_MCP_ARCPY_PYTHON = "C:/Program Files/ArcGIS/Pro/bin/Python/envs/arcgispro-py3/python.exe"
```

### 3. 自检

```powershell
./scripts/check.bat
```

自检会启动 ArcPy worker 并检查环境。先解决路径、ArcPy 导入或许可证错误，再接入客户端；首次加载 ArcPy 可能需要几十秒。

## 客户端接入

以下两种方式任选一种。客户端具体配置文件位置和字段结构以其自身说明为准。

### HTTP：先启动服务，再填写地址

在仓库根目录运行，保持该终端运行：

```powershell
./scripts/run-http.bat
```

在客户端添加 MCP 服务，选择 **Streamable HTTP**，URL 填写 `http://127.0.0.1:8765/mcp`。不是旧版 SSE 端点。

端口被占用时运行 `./scripts/run-http.bat --port 8766`，同时修改客户端 URL。需要固定项目时，可追加 `--project "D:/projects/demo"`；否则连接后让 AI 调用 `use_project`。

### stdio：由客户端启动服务

无需另开 HTTP 服务。在客户端配置以下启动项，把示例中的仓库位置替换为本机实际路径：

```json
{
  "command": "D:/tools/ArcgisMCP/.venv/Scripts/python.exe",
  "args": ["-m", "arcgis_mcp", "--transport", "stdio"],
  "env": {
    "ARCGIS_MCP_PROJECT": "D:/projects/demo"
  }
}
```

这是单个服务的启动参数示例，按客户端要求放入其 MCP 配置中。`ARCGIS_MCP_PROJECT` 可省略，然后由 AI 调用 `use_project`；指定的目录必须存在。`command` 使用独立服务虚拟环境的 Python，ArcPy Python 仍由前面的配置指定。

### 确认接入成功

连接后让 AI 调用 `arcgis_status` 检查环境，再调用 `use_project` 和 `list_datasources` 确认项目与数据源。所有数据路径都针对 **MCP 服务所在电脑**，不是远程客户端的文件系统。

升级代码后重启服务及 worker，客户端重新连接以刷新工具说明。

## 使用方式

先让 AI 选择服务端存在的项目目录，之后使用相对路径：

```python
use_project(folder="D:/projects/demo")
list_gdb_datasets(gdb="./data/City.gdb")
read_gdb_data(gdb="./data/City.gdb", dataset="roads", limit=5)
buffer(in_features="./data/City.gdb/roads",
       out_features="./results.gdb/roads_buffer", distance="100 Meters")
```

写操作默认返回 `job_id`，再查询状态与结果：

```python
job_status(job_id="返回的任务ID")
job_result(job_id="返回的任务ID")
```

任务成功后，用 `result.output` 或 `result.outputs` 中的实际路径继续下一步。`queued` / `running` 时继续查询，不要重复提交；`failed` / `unknown` 时先检查错误和恢复记录。客户端超时不代表后台任务停止。

## 主要功能

| 类别 | 示例工具 |
| --- | --- |
| 项目与状态 | `use_project`、`list_datasources`、`arcgis_status` |
| 读取与描述 | `list_gdb_datasets`、`read_gdb_data`、`read_shp_data`、`describe_data` |
| 矢量处理 | `intersect`、`buffer`、`select`、`dissolve`、`eliminate`、`update_features` |
| 编辑与统计 | `add_field`、`calculate_fields`、`delete_features`、`frequency`、`summary_statistics` |
| 制图与栅格 | `smooth`、`simplify`、`raster_to_polygon`、`reclassify`、`resample` |
| 格式转换 | `export_table`、`table_to_excel`、`features_to_json`、`xy_table_to_point` |
| 历史与任务 | `lineage`、`explain`、`job_status`、`job_result` |

具体参数以客户端获取的工具说明为准。`read_gdb_data` 支持要素类、表，以及已有属性表的整数分类栅格；不自动建立源栅格属性表。

固定工具不足时，可用 `list_arcpy_tools` 搜索、`describe_arcpy_tool` 查询参数。只有 `execution.allowed=true` 的工具才能尝试通过 `run_arcpy_tool` 执行，仍需遵守返回的参数与输出限制。搜索数量不等于可执行数量，也不代表所有参数组合均已实测；未适配工具拒绝执行，任意 ArcPy 代码执行入口禁用。

## 数据保护与链路

- 编辑先复制再操作，保留原件，返回新版本；后续必须使用新路径。
- 显式指定的输出名保持不变；复制编辑自动命名为 `原名_工具名`（如 `D_calculate_fields`），重名加 `_2`、`_3`。覆盖旧版归档为 `原名_overwrite`，删除归档为 `原名_delete_dataset`，同样按序号避让。内部暂存名仍使用随机串；已有历史文件不自动改名。
- 产出写暂存路径，成功后发布。`overwrite=true` 归档旧目标后替换指定路径，默认拒绝覆盖。
- `delete_dataset(confirm=true)` 归档原件并移除原路径；`delete_features` 在副本上删记录。
- 每个 worker 串行执行，同一 GDB 的协作写操作使用工作区锁。ArcGIS Pro 和外部程序仍可能持有冲突锁。
- 同一个 GDB 可保存并连续处理多个要素类，例如 `results.gdb/A`、`results.gdb/A_smooth`；保留每步成果不需要每步新建 GDB。`delete_dataset` 移除原路径但保留归档，`delete_features` 只在副本删记录，不会移除原要素类。
- `lineage(path=...)` 回溯来源，`lineage(path=..., downstream=true)` 查询下游，`explain(path=...)` 解释步骤和中间成果。

项目运行状态保存在服务端 `<项目>/.arcgis-mcp/`：

| 内容 | 用途 |
| --- | --- |
| `history.sqlite3` | 持久化任务结果、操作参数和加工链路 |
| `transactions/` | 输出发布和归档的恢复清单 |
| `scratch/` | 干净 CSV 导出等内部临时数据，正常结束后清理 |

正式成果和保留版本位于对应数据目录或 GDB 中，不是 `scratch/`。链路针对整个数据集，只记录经本 MCP 执行的操作，不自动记录外部手工修改。

当前适合可信本机使用的 Beta 场景。文件发布与审计提交之间的中断对账尚未完善；复杂关联数据的复制编辑会被拒绝。HTTP 未提供应用层用户鉴权，不应直接暴露公网。完整规则、超时和恢复边界见 [数据安全与操作历史](docs/safety-history.md)。

## 开发者如何扩展工具

先读 [AGENTS.md](AGENTS.md) 和 [工具扩展指南](docs/extending-tools.md)。后者提供代码示例、参数约定、注册步骤和测试要求。

| 工具类型 | 接入方式 |
| --- | --- |
| 只读查询 | 继承 `Tool`，调用 worker 中的只读操作 |
| 生成新成果 | 继承 `GeoProcessTool`，在 `core/ops/` 对应功能域注册操作 |
| 修改已有数据 | 继承 `EditTool`，在 `core/ops/attributes.py` 注册复制编辑算法 |
| 扩展通用调用 | 在 `core/ops/meta.py` 增加经验证的安全分类或专用适配器 |

固定工具还需加入 `tools/__init__.py` 的 `ALL_TOOLS`。`tools/` 负责类型化参数和中文说明，`core/ops/` 负责 ArcPy 算法；worker 侧保持 Python 3.9 兼容，不能导入 MCP SDK。

必须复用现有后台任务、复制、暂存发布、工作区锁和持久化审计流程。不要直接修改业务原件、自动删除成功版本、绕过分类执行任意代码，或只加工具名就宣称已安全支持。新增工具要验证实际数据结果及失败行为，并说明未验证范围。

## 测试

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_safety.py
.venv/Scripts/python.exe -X utf8 tests/test_project.py
.venv/Scripts/python.exe -X utf8 tests/test_resolver.py
```

真实 ArcPy 安全检查：`.venv/Scripts/python.exe -X utf8 tests/safety_arcpy_check.py`，需要有效 ArcPy 环境，脚本自行启动 worker。

其他端到端脚本的运行要求见各文件说明。需要样例数据时先调整 `scripts/make-sample-data.bat` 中的 ArcGIS Python 路径，再运行该脚本生成 `data/`。样例数据和 `.arcgis-mcp/` 不随 Git 发布。

## 许可证

Copyright 2026 1834495203.

本项目采用 [Apache License 2.0](LICENSE)。ArcGIS Pro、ArcPy 及其他第三方依赖仍遵循各自许可。
