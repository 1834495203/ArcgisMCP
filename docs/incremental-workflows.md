# 实验、正式步骤与最终流程

新增 [节点图工作流](workflow-graphs.md)：用 `save_workflow_node` 保存代码与 `@节点.输出` 连接，`workflow_nodes` / `list_workflows` 查询，`run_saved_workflow` 执行。本文的单步入口也支持节点引用；`input_mode=read_only` 可显式直接读取更广泛数据类型，原件强制保护依赖部署只读权限。

AI 不必一次写完整条流程：可以实验 → 确认部分步骤 → 再实验 → 继续正式步骤。每次保存独立脚本正文、说明、理由、参数、输入输出与执行记录。

| 模式 | 用途 | 数据与导出 |
| --- | --- | --- |
| `draft`，默认未确认 | AI 自己分析、验证、试算不同方案 | 所有副本和输出进入临时区，不进正式脚本；可 `outputs={}` 只分析 |
| `final`，正式步骤 | AI 决定采用的方法 | 成功后纳入正式流程，数据进入过程或显式交付区；失败、unknown 不导出 |

“确认”是调用方选择 `mode="final"`，不要求每步再向人请求审批。`confirm=true` 仍表示执行可信本机脚本。模式不是执行状态：实验 succeeded 也不是正式成果。

## 一次配置四种数据角色

```text
configure_workflow_data(
  original_roots=["./source.gdb"],
  process_gdb="./process.gdb",
  temporary_gdb="./temporary.gdb",
  delivery_gdb="./delivery.gdb")
```

- 原始数据：保留原路径，SDK 提供副本，不搬动原件。原始根是说明，不是文件系统权限。
- 过程数据：成功正式步骤的输入快照、中间成果，集中在过程库，历史保留。
- 临时数据：实验的输入副本、试算结果，集中在临时库。实验读取正式过程数据时同样先复制到临时区。
- 最终交付数据：只有正式步骤在 `deliverables` 中声明的输出进入交付库，不混入过程和实验副本。

项目固定三个不同的输出 GDB，不按步骤、实验或流程新建库。配置工具只登记，不搬迁已有数据，也不立即建库；开始执行前可修正，执行后固定。使用时按 `auto_create_gdb` 创建缺失库。未配置就首次执行单步入口时使用上述默认库，会话已选工作库可作为过程库。新接口不接收任意输出路径。

Shapefile 按角色进入 `process_files/`、`temporary_files/`、`delivery_files/`。ArcGIS 原生临时数据统一放在有所有权标记的 `.arcgis-mcp/scratch/workflow_native/`，可能自动产生一个内部 scratch.gdb，不在业务目录按步骤创建。

临时是用途分类，不表示立刻删除。当前不自动清理实验数据，避免仍在分析的路径失效；日志与代码保留，正式成功版本不自动删除。旧低阶工具保留兼容。此协议不是 Python 沙箱，不能阻止代码硬编码路径、创建未登记库或绕过 SDK；需要强制只读原件时使用执行账户的文件系统权限。

## 每步只写正文

示例 `select.py`：

```python
arcpy.analysis.Select(inputs["features"], outputs["selected"], parameters["where"])
count = int(arcpy.management.GetCount(outputs["selected"])[0])
if parameters.get("expected_count") is not None:
    step.check("期望行数", count == parameters["expected_count"],
               {"actual": count, "expected": parameters["expected_count"]})
```

运行器注入 `arcpy`、`inputs`、`outputs`、`parameters`、`step`。正文不需要 `workflow.step` 外壳，输入输出使用别名，不硬编码原始路径、临时路径或 GDB。文件作为步骤函数正文执行，自包含，不依赖原文件的 `__file__` 或脚本外部全局状态。

```text
run_workflow_step(
  workflow_id="soil_update", step_id="select_trial", mode="draft",
  script_path="./select.py", description="按类型条件筛选要素",
  reason="先验证 SQL 和命中数量，再决定正式方案",
  inputs={"features":"./source.gdb/A"}, outputs={"selected":"试算筛选要素"},
  input_descriptions={"features":"原始类型图要素"},
  parameters={"where":"VALUE = 1", "expected_count":1}, confirm=true)
```

每次先用 `job_result` 获取终态。可多次实验，同一实验步骤 ID 的不同尝试按 job_id 区分。确定后使用同一正文、原始或正式输入，以 `mode="final"` 和正式步骤 ID 重算。不能直接把临时成果当正式输入；过程/交付区数据也必须有成功正式审计，不能使用失败或未确认的半成品。

正式步骤可接上一正式步骤返回的实际路径。原地编辑须声明 `edit_inputs`：

```text
run_workflow_step(
  workflow_id="soil_update", step_id="calculate_type", mode="final",
  script_path="./calculate.py", description="计算类型字段", reason="采用试验通过的赋值方案",
  inputs={"features":"上一步正式输出的实际路径"}, outputs={"updated":"字段计算后要素"},
  input_descriptions={"features":"正式筛选后待计算字段的要素"},
  edit_inputs={"updated":"features"}, parameters={"field":"VALUE", "value":7}, confirm=true)
```

`calculate.py` 操作 `outputs["updated"]`，服务端已复制对应输入。交付步骤声明 `deliverables=["delivery"]`，通过 `outputs["delivery"]` 写成果。示例正文见 [steps/](../examples/workflows/steps)。

`step.check(name, passed, details, required=True)` 保存 AI 自定验证，passed 必须是 bool。required 验证失败阻止发布，即使脚本捕获异常也不能报告成功；`required=False` 只记录诊断。MCP 不新增面积、数量或零命中的通用业务阈值。

## 查询与导出

```text
workflow_steps(workflow_id="soil_update")
workflow_steps(workflow_id="soil_update", include_drafts=true, detail=true)
export_workflow(workflow_id="soil_update")
```

查询分页，默认只列正式步骤；detail=true 附正文及完整验证。默认验证最多显示 20 条，更多用 job_logs。理由、说明、输入角色、输出声明、实际路径、状态和哈希均可查。有效失败步骤仍保留；提交校验即拒绝的调用可按 job_id 查完整参数和错误。

导出写到项目 `workflows/`，已有导出加序号保留。实验、失败、unknown 步骤不纳入：

| 文件 | 内容 |
| --- | --- |
| `.py` | 可读、可运行代码；每步含说明、理由、参数与输入输出绑定 |
| `.inputs.json` | 原始或外部已确认输入，可换同结构数据；流程内依赖绑定到重跑的新结果 |
| `.parameters.json` | 按步骤 ID 组织的参数，包含 AI 写入参数的验证条件 |
| `.steps.json` | 成功正式步骤的原正文、解释、历史输入输出、角色及验证 |

允许导出已完成部分；尚未声明交付时返回最后一个成功正式步骤的输出。代码不引用旧过程路径或实验残留，但外部其他流程的已确认成果仍作为显式输入，不自动展开其他流程代码。

用 [批量入口或 CLI](workflows.md) 传入导出的 inputs/parameters JSON 重跑。已有布局时复用固定过程库，`output_gdb` 不得指定新库。导出依赖同版本运行器、ArcPy、数据结构及环境，不承诺跨版本数值/OID 完全相同。

单步任务另在 `.arcgis-mcp/workflows/<job_id>/` 保留 `body.py`、`step.json` 和生成包装脚本。SQLite 的 `workflow_units` 保存模式与步骤正文，`operations`、`edges`、`job_events` 保存任务、链路和日志。发布与审计不是一个原子事务；存在未确认步骤时父任务也为 unknown，不能自动重放。

默认 snapshot 输入保真范围仍沿用 `copy_for_edit`：独立要素类、表和 Shapefile；栅格及复杂关联输入拒绝复制。显式 read_only 可直接读取这些对象，日志说明未复制、未隔离；不能据此声称获得保真副本或强制原件保护。验证由 AI 编写。

验证脚本：`tests/test_workflow_plan.py`、`tests/test_workflow.py`、`tests/incremental_workflow_arcpy_check.py`。真实检查只操作独立合成数据，需要有效 ArcGIS Pro/ArcPy。更新后重启服务和 worker，客户端重新连接。
