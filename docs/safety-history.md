# 数据版本、操作审计与任务

新增 [节点图工作流](workflow-graphs.md)，通过引用连接输入输出并保存当前定义、每轮代码快照和独立节点审计。任意 ArcPy 正文不再逐工具分类；默认副本输入保留保真检查，显式 read_only 只读引用不复制，须由部署权限强制保护原件，不能称为沙箱。

所有公开写工具以整个数据集为单位保存加工历史，不追踪单条图斑。

复杂流程新增 `run_workflow` 脚本入口，具体协议见 [脚本工作流](workflows.md)。它复用当前任务、SQLite、工作区锁和发布机制，按步骤记录链路；与下面单工具临时调用不同。脚本执行依赖可信代码遵守 SDK 约定，不提供任意 Python 的写入隔离。

## 未封装工具的临时调用

本节固定封装、`run_arcpy_tool`、`run_unadapted_tool` 仅在服务全局 `tool_profile=legacy` 时注册。默认 `core` 使用受审计工作流脚本，目录查询中的 execution 只是兼容适配元数据，不能据此调用未注册接口。

优先使用固定封装、其次使用已分类的 `run_arcpy_tool`；仍缺少工具时，用独立的 `run_unadapted_tool` 声明本次调用，不会修改永久允许列表。

```text
run_unadapted_tool(
  tool="DeleteIdentical_management",
  reason="固定工具没有按指定字段去重的能力，需要生成去重后的新版本",
  parameters={"in_dataset":"./work.gdb/A", "fields":["TYPE"]},
  input_parameters=["in_dataset"], edit_parameter="in_dataset", confirm=true)
  -> pending/job_id
job_result(job_id=...) -> result.primary_output / result.outputs
job_logs(job_id=..., after=0, limit=20)
explain(path=返回的 primary_output)
```

新产出操作改传 `output_parameter="真实输出参数名"`，并在 `parameters` 中给出输出路径；不能同时传 `edit_parameter`。参数名大小写以 `describe_arcpy_tool` 为准。所有数据集输入都必须列在 `input_parameters`，服务端将每个输入复制并替换实际调用参数，即使调用者认为它只读；所有副本和主输出成功后发布，保留为步骤成果，`primary_output` 是后续应使用的主结果。

首版只支持项目根目录内的独立要素类、表及 Shapefile，单值数据集输入、单个显式要素类/表输出或副本编辑；工具来自已安装的 management、analysis、conversion、cartography、stats、edit 标准工具箱。栅格、工作空间/文件参数、多值数据集、值表、复杂关系数据、多输出和自定义工具箱需专门适配。保真检查失败仍拒绝。输出不能等于输入，已有输出遵循 `overwrite` 归档规则。副本可能增加存储和执行时间。

删除/清空等操作即使作用于副本也需要 `confirm=true`。临时入口不支持 CSV 输出，请使用已适配的表格工具；普通参数只接受已识别的标量类型，坐标系对象、范围/值表等需要专用适配。

使用理由、逻辑参数先随任务持久化；执行过程再写入 SQLite `job_events`：请求、校验、复制、实际 ArcPy 调用及参数映射、ArcPy 消息、发布和失败。`job_logs` 按 sequence 分页，`next_after` 用于下一页。日志写入失败会停止下一阶段；失败调用也保留任务参数与已写日志，成功调用加入现有链路，精简 `lineage` 和 `explain` 同样带理由。普通旧工具任务可能没有详细阶段日志。

这是可信本机客户端的声明执行契约，不是对工具实现或参数表达式的安全沙箱。元数据无法证明无额外副作用，AI 仍需查清工具语义，不能据此承诺任意工具都可靠。多个保留副本的发布以及文件发布与 SQLite 审计不是一个原子事务；发布中断可能留下已发布副本或暂存成果，应结合 `job_result`、`job_logs` 和事务清单检查，不可自动重放。

## 成果保留

- 产出工具先写同一 workspace 的 `mcpwork_<随机号>`，成功且确认输出存在后才改名发布。
- `overwrite=false` 遇到已有成果仍报错。`overwrite=true` 先将旧目标改名归档为 `<名称>_overwrite`，再把成功的新成果发布到指定路径；返回 `target_replaced=true` 和 `previous_output`。交换失败会恢复旧目标，历史成果保留。
- 字段新增、计算、删除、改名、坐标/几何属性、几何修复、追加等编辑工具，统一使用 `arcpy.management.Copy` 复制后修改，再发布新版本。编辑内容不改输入路径；显式覆盖和归档删除按各自声明的路径语义执行。返回的 `source`、`in_features` 保持输入身份，`output` 指向新版本，`source_modified=false`。
- 后续操作必须使用返回的 `output` / `out_features`。继续传原始路径表示另开分支。
- 编辑成果自动命名为 `<输入名>_<操作名>`，如 `D_calculate_fields`。同一路径重复操作时依次选择 `_2`、`_3`，不覆盖前一次成果；固定工具和通用适配器使用同一逻辑操作名。Shapefile 等文件保留扩展名。过长的名称截短输入名部分，保留操作后缀；命名和碰撞检查在工作区锁内完成。显式指定的产出路径不受该规则改变，旧历史不自动改名。
- `delete_dataset(confirm=true)` 把原件改名归档为 `<名称>_delete_dataset`，原路径随即不存在；返回 `original_path_exists=false`。完整数据仍保留在返回的 `output`。覆盖和删除归档名重名时也加 `_2`、`_3`。`delete_features(where=..., confirm=true)` 则先复制后删除命中记录，输入原件不变。通用 Delete/DeleteFeatures/DeleteRows 也有等价适配器。
- 当前实现不提供撤销和自动清理历史成果。失败暂存数据不会作为有效成果列举。

```text
add_field(in_features="./data/X.gdb/A", field="VALUE", field_type="LONG")
  -> output=".../X.gdb/A_add_field"
calculate_fields(in_features=".../X.gdb/A_add_field", assignments={"VALUE": "7"}, where="OBJECTID=1")
  -> output=".../X.gdb/A_add_field_calculate_fields"
explain(path=".../X.gdb/A_add_field_calculate_fields")
```

## 审计

`<项目>/.arcgis-mcp/history.sqlite3` 保存每次写调用的任务编号、会话、完整逻辑参数、开始/结束时间、状态、结果与输入输出边。SQLite 事务保护同一项目的多实例追加。

- `lineage(path, depth=30)`：回溯来源，列出每一步的参数和保留成果。
- `lineage(path, downstream=true)`：查询下游影响面。
- `explain(path, depth=30)`：同时提供按时间排列的中文步骤说明。
- `job_status(job_id)` / `job_result(job_id)`：查询持久化状态/完整结果，不占 ArcPy 执行锁。

项目内路径以相对形式展示，外部来源保留完整路径身份。归档替换后，既有链路的 inputs/outputs 和旧 job 的 output 会指向保留的实际文件；source/in_features 及原始调用参数保持当时的输入身份。每步包含独立 version_id 和输入版本引用；即使外部删除后复用同名路径，回溯也按执行时间选择当时的来源，避免把不同批次的链路混在一起。失败调用也保存，但不产生成功血统边。审计数据库不可写时拒绝开始写操作。原始输入由外部应用手工修改时，MCP 无法记录或阻止该变化，解释中会注明范围。

`lineage` / `explain` 默认精简，保留步骤、输入输出、参数、时间和版本引用，不重复每步完整 schema/result；默认每页 20 步，`offset` / `limit` 翻页（最多 100）。`next_offset` 指示下一页，`truncated` 表示深度限制、`page_truncated` 表示还有下一页。`detail=true` 获取完整步骤，单步原始结果可用 `job_result(job_id, detail=true)` 查询；精简不改变存储的审计。

工具成功表示执行及输出发布成功，不表示业务验收通过。面积差异、融合是否达到目标、零命中是否合理由调用方判断；不会自动全量计算面积或根据要素数未减少判错。实际读取/统计能力供 AI 按需使用。

## 默认工作库

`use_project(folder=..., output_gdb="./工作.gdb", auto_create_gdb=false)` 为当前会话选择默认工作库，不立即创建库；会话隔离，不影响其他客户端。关闭自动建库后，缺失 GDB 的输出请求报错，调用方可显式 `create_gdb` 一次。裸输出数据集名（例如 `A_smooth`）进入该库，显式相对/绝对路径保持不变；编辑仍在源所在工作区生成副本。

需要跨会话保留时，在项目 `.arcgis-mcp/datasources.json` 配置 `"output_workspace": "./工作.gdb"`、`"auto_create_gdb": false`。默认未配置工作库、仍允许自动建库，以兼容已有调用。项目配置覆盖全局配置，会话选择再覆盖项目配置。文件格式如 CSV/Excel/Shapefile 必须使用 GDB 外的目录；不会创建普通文件夹冒充 GDB 要素数据集。

## 超时与队列

写操作先登记任务，默认立即返回任务编号（job_wait_seconds=0）。如配置同步等待，最多等待 5 秒；未完成时返回 `pending=true` 和 `job_id`，客户端应查询结果而非再次提交。断开请求不会取消后台操作，迟到结果不会丢弃。通用工具的元数据查询和路径解析也在后台任务内进行，避免同步预检耗尽 30 秒客户端时限。同一项目、同一会话、相同参数的排队/启动/运行任务会复用同一个 job_id；完成后再次提交表示一次新的执行。

`job_result(wait_seconds=0..5)` 可等待该任务短暂完成，不占 ArcPy 锁；等待到期仍返回实际状态，不把未完成任务判为失败。默认省略重复的调用 args 并精简结果的字段 schema，`detail=true` 恢复完整原始结果。`job_status` 只给状态，不重复任务结果和完整参数。

执行预算默认：读/元数据 60 秒，普通写工具 300 秒，eliminate/union/intersect 1800 秒，dissolve 3600 秒。写操作硬上限为执行预算的两倍，达到上限才终止 worker，并记录 `unknown`。启动预算独立为 180 秒。排队默认最多 60 秒；读调用最多等待执行锁 5 秒。

```json
{
  "read_timeout": 60,
  "call_timeout": 300,
  "startup_timeout": 180,
  "job_wait_seconds": 0,
  "queue_timeout": 60,
  "tool_timeouts": {"eliminate": 1800, "union": 1800, "intersect": 1800, "dissolve": 3600}
}
```

这些配置写在全局 `config/datasources.json`。工具类也可以用 `timeout` 类属性覆盖执行预算。通用调用按解析后的 ArcPy 工具名识别预算。`arcgis_status` 在忙碌时直接返回 current_op、elapsed_seconds 和队列长度，不排队 ping。

worker 意外退出后，只有只读调用可以自动重试。写操作结果不明时不会自动重放。进程重启后，数据库中的历史结果仍可查询；所属进程已退出且未确认完成的任务显示 unknown。

## 写锁与恢复

同一个 worker 串行执行。多个本机实例按规范化后的完整 GDB 路径获取操作系统文件锁，从复制开始一直持有到发布和审计事务提交完成，避免另一实例抢先覆盖、导致历史指向错误。同一 GDB 保守串行写；不同 GDB 使用不同锁。拿不到锁快速返回 WorkspaceBusy。

锁文件默认位于系统临时目录的 `arcgis-mcp-locks`。若不同运行账户、不同临时目录或多台机器访问共享数据，必须设置统一可达的 `ARCGIS_MCP_LOCK_DIR` 并验证该文件系统支持 OS 文件锁；不同路径映射也需要统一。PID 不用于抢占锁，进程退出后 OS 自动释放锁。

ArcGIS Pro 不遵守本项目的协作锁，读图层仍可能占用 schema lock。真实 ArcPy 锁错误会提示关闭相关图层、属性表或编辑会话后重试。

发布前在 `<项目>/.arcgis-mcp/transactions` 写入恢复清单。后续写请求会检查该项目的未完成事务：取得对应 workspace 锁后，将已登记的暂存数据隔离为 `mcpfailed_...`，保留诊断材料，有未完成交换时先恢复归档的原件，将未确认的新成果隔离；不擦除任何完整原件。发布阶段中断的任务需要检查 job_result 和恢复清单，系统不会把未确认成果擅自宣布成功。

## 已支持与明确拒绝的情况

复制编辑支持独立要素类、shapefile 和表。编辑前后检查字段、域引用、子类型、记录数、坐标系/Z/M、索引结构和 GlobalID 值。带附件/关系、属性规则、版本管理、编辑追踪或要素数据集关联的数据尚未验证，拒绝复制编辑，避免静默丢失约束。

`run_arcpy_tool` 使用明确的安全分类：常用产出工具走暂存发布，支持的编辑工具走复制适配器，派生输出不当作可覆盖输出。未分类工具在此入口拒绝；可增加永久适配器，或满足上文 `run_unadapted_tool` 的有限临时声明契约后使用独立入口。`CreateFeatureclass` / `CreateTable` / `CreateFileGDB` / `TableToTable` 的目录和名称组合也按实际数据集发布；工作空间相对路径按项目根目录解析。

`list_arcpy_tools` 和 `describe_arcpy_tool` 返回共用执行分类的 `execution`：`allowed` 表示已有适配器，`mode` 表示只读、复制编辑、暂存发布或归档删除，`constraints` 列出限制。编辑适配器的 `supported_parameters` 明确可用参数；其他 ArcPy 参数不因能查询到就获准使用。`validation=not_certified_for_all_parameters` 表示不承诺所有参数组合均已实测。搜索的 `total_matched` 只是本机匹配目录数，`classified_matched` 才是其中已分类数（均在截断前统计）；两者都不代表逐工具验证数量。固定封装优先，通用调用仍需核验坐标系、单位、许可证和结果。

任意 Python 代码无法保证不绕过副本、锁和审计，所以审计保留模式下 `run_arcpy_code` 不执行代码，即使旧配置允许注册该工具。

## 栅格属性表和统计输出

`read_gdb_data` 支持已有属性表的整数分类栅格，返回 Value、Count 等字段，以及 where / fields / 分页结果。不自动建立或修改源栅格属性表；无属性表的栅格返回明确提示。

`frequency`、`summary_statistics`、`export_table` 的 CSV 默认 clean_csv=true：先在私有 GDB 表中处理，再流式写出单个 UTF-8 CSV，不生成 CSV XML 或 schema.ini。设置 clean_csv=false 可使用 ArcPy 原生输出和类型元数据；发布/归档时相应 schema.ini 节也会随文件名调整。需要字段类型保真时，直接将统计输出写到 `./结果.gdb/表名`；CSV 本身不携带字段类型。

`zonal_statistics` 也支持同样的干净 CSV 输出，`check_geometry` 的 CSV 经同一流程导出。内部工作区带 `.mcp-scratch.json` 所有权标记，游标引用释放后清理，失败保留标记供后续写请求重试。`arcgis_status` 的 `scratch` 可查看清理错误与未标记旧目录数量；未标记旧目录不会被自动删除，成功版本不属于临时区。

`read_gdb_data`、`read_shp_data` 和 `describe_data` 默认字段只回 `name` / `type`，`schema_detail=true` 返回长度、精度等完整字段元数据。`raster_to_polygon` 的 `raster_field` 是输入分类字段，实际输出名用 `output_value_field` 查询，可能为 `gridcode`；返回实际生效的简化/多部件选项，不擅自评价精度。矢量掩膜按像元中心取舍，不能等同于矢量边界精确裁剪。

频数表一行表示所有分组字段的完整取值组合，FREQUENCY 表示输入中该组合的记录数；融合后每个组合只有一个要素时，全为 1 正常。

消除后普通 AREA_MU 等属性不自动重算。返回值会提示先对新 output 重算面积，再按面积字段执行下一遍；eliminated_count 为实际减少的记录数，selected_count 为选中数，两者可能不同。

`arcgis_status` 和工具结果中带 `data_policy_version=archive-replace-4`，用于识别旧进程。更新后应重启 MCP 服务及其 worker，客户端重新连接刷新参数协议。

## 验证

无 ArcPy 回归：`.venv/Scripts/python.exe -m unittest discover -s tests -p test_safety.py -v`

真实 ArcPy 集成：`.venv/Scripts/python.exe -X utf8 tests/safety_arcpy_check.py`。只在仓库下新建临时测试项目，退出后清理测试数据，不接触已有业务数据。
