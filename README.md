# ArcgisMCP

通过本机 ArcPy 向 AI 客户端提供 ArcGIS 数据读取、地理处理和加工链路查询，支持 Streamable HTTP 与 stdio。

## 环境与安装

需要 Windows、可用的 ArcGIS Pro / ArcPy 环境及相应工具许可，以及独立的 Python 3.10+（推荐 3.12）。MCP 服务和 ArcPy worker 使用不同解释器；不要向服务虚拟环境安装 ArcPy。

```powershell
git clone https://github.com/1834495203/ArcgisMCP.git
cd ArcgisMCP
./scripts/install.bat
```

修改 `config/datasources.json` 的 `arcpy_python`，指向本机 ArcGIS Python，例如：

```json
{
  "arcpy_python": "C:/Program Files/ArcGIS/Pro/bin/Python/envs/arcgispro-py3/python.exe"
}
```

上面只展示需要修改的字段，保留配置文件其他设置。也可通过环境变量 `ARCGIS_MCP_ARCPY_PYTHON` 覆盖。

```powershell
./scripts/check.bat
./scripts/run-http.bat
```

默认地址：`http://127.0.0.1:8765/mcp`。在支持 HTTP MCP 的客户端中填写该地址。需要其他端口时运行 `scripts/run-http.bat --port 8766`。

stdio 客户端可将命令设为仓库内 `.venv/Scripts/python.exe` 的绝对路径，参数设为 `-m arcgis_mcp --transport stdio`；也可使用 `scripts/run-stdio.bat`。先完成上述安装。

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
- 产出写暂存路径，成功后发布。`overwrite=true` 归档旧目标后替换指定路径，默认拒绝覆盖。
- `delete_dataset(confirm=true)` 归档原件并移除原路径；`delete_features` 在副本上删记录。
- 每个 worker 串行执行，同一 GDB 的协作写操作使用工作区锁。ArcGIS Pro 和外部程序仍可能持有冲突锁。
- `lineage(path=...)` 回溯来源，`lineage(path=..., downstream=true)` 查询下游，`explain(path=...)` 解释步骤和中间成果。

项目运行状态保存在服务端 `<项目>/.arcgis-mcp/`：

| 内容 | 用途 |
| --- | --- |
| `history.sqlite3` | 持久化任务结果、操作参数和加工链路 |
| `transactions/` | 输出发布和归档的恢复清单 |
| `scratch/` | 干净 CSV 导出等内部临时数据，正常结束后清理 |

正式成果和保留版本位于对应数据目录或 GDB 中，不是 `scratch/`。链路针对整个数据集，只记录经本 MCP 执行的操作，不自动记录外部手工修改。

当前适合可信本机使用的 Beta 场景。文件发布与审计提交之间的中断对账尚未完善；复杂关联数据的复制编辑会被拒绝。HTTP 未提供应用层用户鉴权，不应直接暴露公网。完整规则、超时和恢复边界见 [数据安全与操作历史](docs/safety-history.md)。

## 测试与扩展

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_safety.py
.venv/Scripts/python.exe -X utf8 tests/test_project.py
.venv/Scripts/python.exe -X utf8 tests/test_resolver.py
```

真实 ArcPy 安全检查：`.venv/Scripts/python.exe -X utf8 tests/safety_arcpy_check.py`，需要有效 ArcPy 环境，脚本自行启动 worker。

其他端到端脚本的运行要求见各文件说明。需要样例数据时先调整 `scripts/make-sample-data.bat` 中的 ArcGIS Python 路径，再运行该脚本生成 `data/`。样例数据和 `.arcgis-mcp/` 不随 Git 发布。

扩展工具请先读 [AGENTS.md](AGENTS.md) 和 [工具扩展指南](docs/extending-tools.md)，复用现有任务、复制、发布、锁和审计流程。代码更新后重启服务及 worker，客户端重新连接刷新工具说明。

## 许可证

Copyright 2026 1834495203.

本项目采用 [Apache License 2.0](LICENSE)。ArcGIS Pro、ArcPy 及其他第三方依赖仍遵循各自许可。
