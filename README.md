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

复杂流程可让 AI 用 `save_workflow_node` 保存 Python 节点，通过 `@节点.输出` 连接；`list_workflows` / `workflow_nodes` 查询，`run_saved_workflow` 运行，`export_workflow` 导出脚本。每轮记录代码、说明、输入输出和日志，不必逐个封装 ArcPy 算法。用法与原始数据保护边界见 [节点图工作流](docs/workflow-graphs.md)。默认副本输入；显式 `read_only` 可直接读取更广泛数据，但强制保护原件需部署执行账户只读权限。

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

可用 `job_result(job_id="...", wait_seconds=5)` 短暂等待，减少轮询。默认精简字段元数据，`detail=true` 查看完整原始结果。

需要固定工作库时，可以这样选择（只影响当前会话，不在此处建库）：

```python
use_project(folder="D:/projects/demo", output_gdb="./工作.gdb", auto_create_gdb=False)
```

工作流使用 `configure_workflow_data` 配置固定角色库，允许自动建库时在首次使用时创建。若关闭自动建库，应先准备好这些库；默认没有独立 `create_gdb` 工具。兼容模式中的产出工具支持裸名称写入默认工作库。跨会话配置可写入项目 `.arcgis-mcp/datasources.json`：

```json
{"output_workspace": "./工作.gdb", "auto_create_gdb": false}
```

## 主要功能

默认仅注册 **24 个核心工具**，GIS 算法通过受审计工作流节点执行。

| 类别 | 数量 | 工具 |
| --- | ---: | --- |
| 项目、读取与描述 | 8 | `use_project`、`list_datasources`、`arcgis_status`、`describe_data`、`list_gdb_datasets`、`read_gdb_data`、`list_shp_datasets`、`read_shp_data` |
| 历史与任务 | 5 | `lineage`、`explain`、`job_status`、`job_result`、`job_logs` |
| 工作流 | 9 | `configure_workflow_data`、`save_workflow_node`、`workflow_nodes`、`list_workflows`、`run_saved_workflow`、`run_workflow_step`、`workflow_steps`、`run_workflow`、`export_workflow` |
| ArcPy 目录 | 2 | `list_arcpy_tools`、`describe_arcpy_tool` |

具体参数以客户端获取的工具说明为准。`read_gdb_data` 支持要素类、表，以及已有属性表的整数分类栅格；不自动建立源栅格属性表。

`read_*` / `describe_data` 的字段默认仅返回名称与类型，`schema_detail=true` 查看完整结构。`lineage` / `explain` 默认精简分页，用 `next_offset` 继续、`detail=true` 查看完整步骤。

用 `list_arcpy_tools` 搜索、`describe_arcpy_tool` 查询参数，然后在节点正文中编写 ArcPy 代码。返回的 `execution` 是旧兼容适配器状态，不代表该 MCP 执行接口已注册，也不限制节点脚本能使用的算法。

需要旧客户端接口时，在服务全局 `config/datasources.json` 设置 `"tool_profile": "legacy"`，或用环境变量 `ARCGIS_MCP_TOOL_PROFILE=legacy` 覆盖。重启后额外注册 58 个固定 GIS 工具和 `run_arcpy_tool`、`run_unadapted_tool`，共 84 个；默认 `core`。这是启动配置，不随项目或会话切换。旧 `allow_arcpy_code` 显式开启时还会注册一个拒绝执行代码的兼容入口，不建议启用。

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

**MCP 负责执行正确、输出可用和数据保护；业务验证由 AI/调用方负责。** 不会因融合数量没减少或条件零命中判失败，也不会自动按业务阈值核验面积；需要时由 AI 调用读取、统计工具检查。

## 开发者如何扩展工具

推荐使用 **`run_workflow_step`** 分步推进：先 `draft` 做分析与实验，再把采用的方案以 `final` 重算，期间可继续实验。每步保存代码、说明、理由、输入输出及 AI 自定的 `step.check` 验证；`workflow_steps` 查询，`export_workflow` 将成功正式步骤组合成可运行脚本及输入、参数、说明清单。

`configure_workflow_data` 一次固定原始、过程、临时、交付数据布局。实验数据只进临时区，正式过程与交付分库；步骤不接收任意输出路径，不按步骤创建新 GDB。正式步骤不能依赖实验残留。详见 [分步实验、正式流程与导出](docs/incremental-workflows.md)。

复杂流程现在可直接编写 Python 脚本，通过 **`run_workflow`** 提交，或用 `python -m arcgis_mcp.workflow` 在命令行运行。提交时保存脚本快照和理由；输入先复制，脚本通过步骤 SDK 登记依赖、生成输出或复制编辑，成功中间成果保留。`job_logs` 查过程，`lineage` / `explain` 查逐步链路。原有工具保留兼容，不再要求为每个算法新增 MCP 封装。详见 [脚本工作流与示例](docs/workflows.md)。

脚本运行器面向可信本机代码，**不是安全沙箱**；硬编码原始路径或绕过 SDK 的写入无法全面拦截。首版输入快照仍受现有数据保真限制，不支持栅格及复杂关联输入。

仅在 `legacy` 模式中，可使用 `run_arcpy_tool` 的已适配调用或 `run_unadapted_tool` 的有限临时声明。固定算法封装也仅在此模式注册，规则见 [兼容临时调用说明](docs/safety-history.md#未封装工具的临时调用)。新增流程优先节点脚本。

`job_logs(job_id)` 查询持久化的理由、实际调用、路径映射、ArcPy 消息和阶段日志；`lineage` / `explain` 查询成功的加工链路。这些记录也保存在 `history.sqlite3`，服务重启后仍可查。

先读 [AGENTS.md](AGENTS.md) 和 [工具扩展指南](docs/extending-tools.md)。后者提供代码示例、参数约定、注册步骤和测试要求。

| 工具类型 | 接入方式 |
| --- | --- |
| 只读查询 | 继承 `Tool`，调用 worker 中的只读操作 |
| 生成新成果 | 继承 `GeoProcessTool`，在 `core/ops/` 对应功能域注册操作 |
| 修改已有数据 | 继承 `EditTool`，在 `core/ops/attributes.py` 注册复制编辑算法 |
| 扩展通用调用 | 在 `core/ops/meta.py` 增加经验证的安全分类或专用适配器 |

固定工具加入 `tools/__init__.py` 的 `ALL_TOOLS` 后默认归入兼容组；新增核心接口还需明确加入 `CORE_TOOLS`，不要无意扩大默认列表。`tools/` 负责类型化参数和中文说明，`core/ops/` 负责 ArcPy 算法；worker 侧保持 Python 3.9 兼容，不能导入 MCP SDK。

必须复用现有后台任务、复制、暂存发布、工作区锁和持久化审计流程。不要直接修改业务原件、自动删除成功版本、绕过分类执行任意代码，或只加工具名就宣称已安全支持。新增工具要验证实际数据结果及失败行为，并说明未验证范围。

## 测试

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_safety.py
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_execution_contracts.py
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_unadapted.py
.venv/Scripts/python.exe -X utf8 tests/test_project.py
.venv/Scripts/python.exe -X utf8 tests/test_resolver.py
```

真实 ArcPy 安全检查：`.venv/Scripts/python.exe -X utf8 tests/safety_arcpy_check.py`，需要有效 ArcPy 环境，脚本自行启动 worker。

其他端到端脚本的运行要求见各文件说明。需要样例数据时先调整 `scripts/make-sample-data.bat` 中的 ArcGIS Python 路径，再运行该脚本生成 `data/`。样例数据和 `.arcgis-mcp/` 不随 Git 发布。

## 许可证

Copyright 2026 1834495203.

本项目采用 [Apache License 2.0](LICENSE)。ArcGIS Pro、ArcPy 及其他第三方依赖仍遵循各自许可。
