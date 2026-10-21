# 用脚本执行工作流

保存和运行节点图见 [节点图工作流](workflow-graphs.md)。批量入口新增 `input_mode="read_only"`（CLI `--input-mode read_only`），可直接只读引用 ArcPy 数据/工作空间，绕开单数据集快照的类型限制；它不提供权限隔离，原件强制保护需部署只读权限。默认 snapshot 保持现有保真检查。

**交互式分步开发优先使用 [实验、正式步骤与最终流程](incremental-workflows.md)**：`run_workflow_step` 支持 draft/final、逐步理由与输入输出、AI 自定验证、四类数据角色及正式脚本导出。本文保留批量脚本/导出脚本的运行协议。已有项目布局时始终复用固定过程库，不再允许每次 `output_gdb` 换库。

复杂流程优先由 AI 写成脚本，通过 `run_workflow` 或命令行提交。MCP 继续提供读取、统计、任务和历史查询，不必为每个算法新增封装。现有固定工具和通用入口保留兼容。

这是新增的可信本机脚本执行入口，不是文件系统或 Python 沙箱。旧 `run_arcpy_code` 仍禁用。脚本必须遵守下面的步骤协议；绕过 SDK、硬编码原始路径写数据、修改共享域或其他未登记副作用，运行器无法全面拦截或追踪。需要强制保护时，由部署者给执行账户设置原数据只读、工作目录可写的权限，不能把 `confirm` 当作权限隔离。

## 脚本约定

脚本中自动注入 `workflow`，自行 `import arcpy`。使用 `workflow.inputs` 获取已经保真复制并保留的输入快照，使用 `workflow.parameters` 获取运行参数。不要在脚本中重新读取或修改原始路径。

```python
import arcpy

source = workflow.inputs["features"]

with workflow.step("select", inputs=[source],
                   parameters={"where": workflow.parameters["where"]}) as selection:
    arcpy.analysis.Select(source, selection.output(), workflow.parameters["where"])

# 退出步骤后才能使用最终路径，不能把暂存路径传给下一步。
selected = selection.result

with workflow.step("calculate", inputs=[selected],
                   parameters={"value": workflow.parameters["value"]}) as calculation:
    editable = calculation.edit_copy(selected)
    arcpy.management.CalculateField(editable, "VALUE",
                                   str(workflow.parameters["value"]), "PYTHON3")

workflow.set_outputs({"final": calculation.result})
```

完整示例见 [select_calculate.py](../examples/workflows/select_calculate.py)。只读统计可直接通过 MCP 执行；需要形成数据集链路的脚本步骤必须登记输出。

| SDK 接口 | 语义 |
| --- | --- |
| `workflow.inputs[alias]` | 输入保留副本，不是原始路径 |
| `workflow.parameters` | 提交时保存的参数对象 |
| `workflow.step(name, inputs, parameters)` | 声明一个逻辑步骤，输入只能来自快照或已完成步骤；建议使用稳定的英文步骤 ID |
| `step.output()` | 申请工作库中的暂存输出；成功后生成可读名称，重跑时加序号 |
| `step.output("name")` | 显式指定合法 GDB 数据集名称，已有同名成果拒绝，不覆盖 |
| `step.edit_copy(source)` | 复制已声明输入后返回可编辑的暂存路径，源版本保留 |
| `step.result` | 单输出步骤提交后的最终路径；多输出时为 `None` |
| `step.outputs` | 步骤提交后的全部最终路径列表 |
| `workflow.set_outputs({name: path})` | 指定已成功登记的最终成果；退出脚本前必须调用 |

一个步骤可登记多个输出，但发布是逐项进行的，不是跨数据集原子事务；不接受未登记的隐式输出。失败后不自动清理成功成果，也不提供整条流程回滚。每次原地编辑都必须调用 `edit_copy`，不要修改前一步已保留的数据。

## MCP 提交与查询

先选择项目。已有布局时固定复用过程库，不再允许 output_gdb 指向新库。仅旧批量入口首次使用且尚无布局时，选择会话工作库或 `<项目>/work.gdb` 作为过程库。缺失库是否自动创建仍由 `auto_create_gdb` 控制。

```text
run_workflow(
  script_path="./process.py",
  reason="按条件筛选后计算字段，保存完整可重跑流程",
  inputs={"features":"./source.gdb/A"},
  parameters={"where":"VALUE = 1", "value":7},
  output_gdb="./work.gdb",
  timeout_seconds=3600,
  confirm=true)
  -> job_id / pending
job_result(job_id=...)
job_logs(job_id=..., after=0, limit=20)
lineage(path=最终 result.outputs 中的 path)
```

`timeout_seconds` 为 10~86400 秒，含脚本子进程启动和输入快照准备。父任务不添加覆盖整个流程的链路边，每个成功步骤单独登记。父任务 `result.steps` 列出步骤任务 ID；失败时从 `job_logs` 的 `step_started` 事件查询已执行步骤，然后用 `job_result` 查看它们。

客户端超时不代表脚本已停止。脚本异常时保留成功步骤；强制终止、监督进程消失或发布/审计中断可能导致 `unknown`，不得自动重放。运行器会在正常超时时终止并等待子进程；子进程还检测监督进程是否存在。这不是针对恶意脚本或任意子孙进程的隔离措施。

## 命令行提交

`inputs.json` 保存别名到输入路径的对象，`parameters.json` 保存脚本参数。两种入口使用同一脚本快照、后台任务、ArcPy worker、SDK、安全层和 SQLite 历史。

```powershell
.venv/Scripts/python.exe -X utf8 -m arcgis_mcp.workflow ./process.py --project D:/GIS/project --inputs ./inputs.json --parameters ./parameters.json --reason "生成本次成果" --output-gdb ./work.gdb --confirm
```

命令行会等待并输出终态，失败返回非零退出码。不要用 `python process.py` 代替运行器来要求同等审计；脚本的 `workflow` 由运行器注入。

## 保存内容与复现边界

提交时读取脚本内容并保存于任务参数；排队期间修改源文件不影响已提交任务。运行时在 `<项目>/.arcgis-mcp/workflows/<job_id>/` 保存 `script.py`、`request.json`、`stdout.log`、`stderr.log`、`outcome.json`，同时保存 SHA-256 校验值。Python 标准输出、标准错误、步骤参数、ArcPy 消息、环境信息及异常也写入现有 SQLite 的阶段日志。原生输出留在日志文件中，不保证每条都进入 SQLite。

每个步骤拥有独立任务 ID，包含父任务 ID 和脚本哈希；成功步骤连接真实输入、输出，`lineage` / `explain` 能回溯全部已登记中间成果。脚本失败不会抹去先前步骤。结果、文件发布、阶段日志、SQLite 提交不是同一个原子事务；故障后须结合任务状态和 `transactions/` 检查已发布但未成功登记的成果。

脚本快照目前只保存提交的单个 Python 文件，不打包外部辅助模块、第三方依赖或数据内容哈希。记录 Python/ArcGIS 版本、部分初始环境及每步结束时的环境值，不自动恢复整个环境。要复现流程，应使用自包含脚本，显式设置坐标系、单位、范围、栅格对齐、随机种子和并行参数，固定依赖版本，并使用保留的输入快照。重跑后的路径和 OID 不承诺相同；业务和数值一致性仍由调用方验证。

首版输入快照沿用 `copy_for_edit`：支持通过保真检查的独立要素类、表和 Shapefile；栅格、附件、关系、属性规则等复杂输入仍拒绝。Shapefile 按角色保留在 process_files/temporary_files/delivery_files，GDB 成果进入对应角色库。新产出可用 `step.output` 登记，但具体算法的输出格式、隐式修改、许可证和副作用仍由脚本作者确认，不表示所有 ArcPy 工具都经过适配验证。

## 验证与上线

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_workflow.py
.venv/Scripts/python.exe -X utf8 tests/workflow_arcpy_check.py
```

真实检查仅创建独立合成测试项目，需要可用 ArcGIS Pro 与许可证。修改后重启 MCP 服务和 worker，客户端重新连接刷新 schema。
