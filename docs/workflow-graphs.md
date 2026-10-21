# 用节点引用组织 ArcPy 工作流

MCP 管理流程图、代码快照、输入输出引用、任务和节点审计。AI 编写节点内的 Python，可以使用本机已安装且具备许可证的 ArcPy 能力，不再要求每个算法都有固定适配器。节点串行执行，不新增并行写 worker 池。

## 保存、查询与运行

`save_workflow_node` 保存或更新定义，不执行，也不改历史代码快照。允许先保存下游；运行和导出前统一检查缺失输出、缺失节点及循环。

```text
save_workflow_node(
  workflow_id="soil", step_id="select",
  description="筛选目标类型", reason="提取本次处理范围",
  inputs={"features":"./source.gdb/A"}, input_descriptions={"features":"原始类型图"},
  outputs={"selected":"筛选结果"}, parameters={"where":"VALUE = 1"},
  code='arcpy.analysis.Select(inputs["features"], outputs["selected"], parameters["where"])')

save_workflow_node(
  workflow_id="soil", step_id="calculate",
  description="计算字段", reason="采用试验确认的赋值方法",
  inputs={"features":"@select.selected"}, input_descriptions={"features":"上游筛选结果"},
  outputs={"updated":"计算后结果"}, edit_inputs={"updated":"features"},
  deliverables=["updated"],
  code='arcpy.management.CalculateField(outputs["updated"], "VALUE", "7", "PYTHON3")')

list_workflows()
workflow_nodes(workflow_id="soil", detail=true)
run_saved_workflow(workflow_id="soil", reason="运行已保存的类型更新流程", confirm=true)
```

`@select.selected` 表示本流程 select 节点的 selected 输出。运行时绑定到本轮新结果，不引用旧实验路径。`inputs` 为外部路径或节点引用，`outputs` 的值是说明。成果自动用节点和输出别名命名，碰撞加数字；过程、临时和交付库仍由 `configure_workflow_data` 统一配置。

步骤内可自由创建、编辑或删除本节点工作副本中的内容，用 `step.check` 保存自行编写的验证。`edit_inputs` 自动提供副本，不直接改原件或上游已发布成果。此保留版本方式避免分支相互污染，并保留每一步结果；不要求过程算法逐个适配。声明多项 outputs 即可产生多个数据集，全部存在后才进入发布；文件发布与审计并非原子事务。

纯分析、探索继续使用 `run_workflow_step(mode="draft")`；该入口现在也支持 `inputs={"features":"@select.selected"}`，从本流程模式兼容的成功步骤取实际路径。保存的图是正式可复用定义，不自动纳入实验节点。更新定义不会自动重跑，也不影响已经提交的任务。

`run_saved_workflow(inputs={"select__features":"./another.gdb/A"}, parameters={"select":{"where":"VALUE = 2"}}, ...)` 可更换外部输入和节点参数。参数覆盖以整个节点参数对象为单位。没有显式交付时，返回所有终端节点输出；有交付声明时只返回声明的交付。

## 原始数据与任意 Python 的边界

| input_mode | 行为 | 限制 |
| --- | --- | --- |
| `snapshot`，默认 | 先做保真副本再传给脚本 | 支持通过现有检查的独立要素类、表、Shapefile；复杂关联输入仍拒绝 |
| `read_only`，显式选择 | 将原始输入以只读约定直接绑定给节点，不复制 | 可引用 ArcPy 能读取的栅格、工作空间、复杂数据等；运行器不强制只读权限，也不自动保存原件内容快照 |

两种模式都不是 Python 沙箱。要在任意代码下**强制确保原始数据不能修改**，必须将原始数据放在执行身份无写权限的位置，由部署者配置 Windows/存储权限。不能用文件只读属性或 `confirm=true` 代替访问控制。File GDB 某些读取操作也可能需要锁文件权限，须在目标 ArcGIS 环境验证；确有冲突时使用完整的独立工作副本，不能临时放开原件写权限后宣称受保护。项目角色配置不会自动更改 ACL。

`read_only` 的日志明确记录未复制、未提供运行时隔离；不宣称已验证原件未变。SDK 不提供写回只读引用的接口，节点应只修改 outputs 或自行创建的工作数据。`edit_inputs` 仍使用原有保真复制，不会因 read_only 而绕过检查。复杂关联加工需由作者选择完整工作副本和适当的工作空间策略；直接读取复杂数据不等于可自动保真复制它。

任意代码中未登记的路径、工具隐式副作用、外部系统访问不能自动形成完整血缘。输出当前仍通过现有数据集暂存发布流程管理，不承诺任意工作空间结构、关联数据、多文件输出都可自动发布。脚本可调用的工具范围比自动管理的数据范围更广。

## 审计、导出和复跑

- `job_result` / `job_logs` 查询执行；`workflow_steps` 列出每轮实际节点、输入输出、代码、参数和验证；`workflow_nodes` 查询当前定义，两者分开。
- 已保存图运行时额外保存 `graph.json`；每个执行节点独立提交到 SQLite。更新定义不改变历史快照。父任务失败时保留先前成功节点；unknown 不自动重放。
- `export_workflow(workflow_id="soil", source="definition")` 导出当前定义，即使未运行；不代表验证通过。
- `export_workflow(workflow_id="soil", source="executed")` 默认导出成功正式节点；保存图多次运行时只取最近一轮的成功节点，避免跨轮混合。该轮仍在运行或 unknown 时拒绝导出，即使还没有产生节点记录；不自动补齐失败节点，也不默默退回上一轮。
- 产出 `.py`、`.inputs.json`、`.parameters.json`、`.steps.json`。用 `run_workflow` 或 `python -m arcgis_mcp.workflow` 重跑；read_only 输入须显式传同名选项或 `--input-mode read_only`，不会因为导出而隐式降低保护。

导出保留依赖、正文、理由和生效参数，绑定重跑结果。依赖本机 ArcPy、运行器、许可证和外部环境；不承诺任意 Python 的外部依赖可自动打包，或跨版本/OID/数值逐位一致。
