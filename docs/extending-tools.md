# 工具扩展指南

本指南供贡献者和 AI 按需封装 ArcPy 工具。先读根目录 [AGENTS.md](../AGENTS.md)；运行语义见 [安全、任务与历史说明](safety-history.md)。示例代码用于说明接入位置，不表示仓库已经新增该工具或完成实测。

## 先确认需求，再选入口

默认 `tool_profile=core` 仅暴露 24 个项目/读取、任务/审计、工作流与目录接口。固定封装和两个通用执行入口仅在全局 `tool_profile=legacy` 注册，共 84 个。新增固定封装加入 `ALL_TOOLS` 会自动进入兼容组；只有确有必要的核心接口才同时加入 `CORE_TOOLS`。更改启动配置需重启并重新连接，项目配置不改变注册列表。

可复用流程优先 [节点图工作流](workflow-graphs.md)：保存节点正文，输入通过引用连接，MCP 管理执行、日志与导出。read_only 输入扩展可读数据范围，但强保护依赖部署权限；它不会放宽固定适配器或 edit_copy 的保真检查。

交互式试验和逐步确认使用 [增量工作流](incremental-workflows.md)，新增算法通常只需编写步骤正文，不需要新增 MCP 工具。不得绕过 draft/final、项目角色库、输入保护、逐步审计与导出依赖绑定；验证条件由 AI 编写并记录。

复杂或一次性的多步骤流程优先编写 [受审计脚本工作流](workflows.md)，通过步骤 SDK 复用任务、复制、发布和历史。固定封装主要服务常用读取、统计及少量稳定编辑，不追求全量 ArcPy 适配。需要特殊数据保护或共享结构修改时，脚本也不能绕过保真和副作用设计。

开始前写清五件事：ArcPy 实际工具名、输入数据类型、全部输出、是否隐式修改输入、需要暴露的参数。查看目标安装上的 `describe_arcpy_tool`，核对对应版本的 Esri 文档及许可证；元数据只能描述参数，不能证明工具没有副作用。

| 需求 | 推荐方式 | 需要检查 |
| --- | --- | --- |
| 常用操作，需要中文说明、默认值或参数校验 | 新增固定 MCP 工具 | 参数类型、单位、真实执行语义 |
| 固定工具不够用的单输出产出工具 | 为通用调用增加安全分类 | 输入不变、显式输出、暂存路径兼容、无隐藏输出 |
| ArcPy 原地改字段、几何或记录 | 新增 `EditTool` 和编辑操作 | 所有修改只作用于副本，数据约束保真 |
| 只读查询 | `Tool` + worker 只读操作 | 不自动建立索引、统计表或修改源数据 |
| 多输出、修改工作空间、关联数据或外部系统 | 专门设计适配器 | 全部输出/副作用、锁、审计和失败恢复，不能直接加白名单 |

固定工具和通用调用的支持范围是两条独立接入路径：增加固定工具不会自动开放同名通用工具，反之亦然。

## 数据从哪里经过

```text
MCP 方法：类型化参数、中文协议
  → GeoProcessTool.run_geoprocess / EditTool.run_edit
  → resolver：项目路径解析
  → Tool.arcpy：提交后台任务并持久化 job_id
  → Jobs → worker：串行执行
  → op_geoprocess / op_edit：工作区锁、暂存输出或复制编辑
  → ops 中的实际 ArcPy 操作
  → 安全层发布成果 → SQLite 提交结果与链路 → 释放锁
  → AI 用 job_status / job_result 取得最终路径
```

基础设施承担锁、发布、任务和审计，操作函数只做当前算法。不要在工具方法里直接启动 ArcPy 子进程，也不要在操作函数里重复发布、重复复制或改写整个历史库。

## 新增固定产出工具

下面以尚未固定封装的 `pairwise_buffer` 为结构示例。示例只开放距离参数；其余 ArcPy 参数使用目标版本默认值，是否足够应由实际需求和测试决定。

在 `src/arcgis_mcp/core/ops/vector.py` 增加操作并注册：

```python
def _pairwise_buffer(inputs, out, params):
    import arcpy

    distance = str(params.get("distance") or "").strip()
    if not distance:
        raise WorkerError("distance 不能为空，例如 100 Meters")
    arcpy.analysis.PairwiseBuffer(inputs[0], out, distance)


register("pairwise_buffer", "成对缓冲区", _pairwise_buffer,
         min_inputs=1, allowed={"distance"})
```

`out` 是安全层给的暂存路径。函数不得用用户原始输出路径替换它，也不得打开全局 `overwriteOutput=True`、自行发布或删除输入。需要额外返回信息时返回字典；不能覆盖公共结果中的逻辑输入、最终路径或状态。

在 `src/arcgis_mcp/tools/analysis.py` 增加封装：

```python
class PairwiseBuffer(GeoProcessTool):
    name = "pairwise_buffer"
    operation = "pairwise_buffer"

    def pairwise_buffer(self, ctx: Context, in_features: str,
                        out_features: str, distance: str,
                        overwrite: bool = False) -> str:
        """创建缓冲区，保留输入，生成新成果。

        distance: 距离及单位，例如 100 Meters；坐标系与测距方法需核验。
        overwrite: true 时归档旧目标后替换指定输出路径。
        提交后按 job_id 查询最终结果；后续使用 result.output。
        """
        return self.run_geoprocess(
            ctx, in_features=[in_features], out_features=out_features,
            params={"distance": distance}, overwrite=overwrite,
        )
```

上述文件已有相应基类、`Context`、`WorkerError` 和 `register` 的导入或定义；新建模块时显式补齐。然后在 `tools/__init__.py` 导入类并加入 `ALL_TOOLS`。现有 ops 功能域的 `OPERATIONS` 会自动汇总；新建 ops 模块则还需加入 `core/ops/__init__.py` 的汇总。

额外输入路径不要埋在未经解析的 `params` 中。优先纳入 `in_features` / `extra_inputs` 并约定顺序；需要不同角色时参考现有 `UpdateFeatures`、`Append`。验证最终 `inputs` 和 `lineage` 确实包含所有依赖。

## 新增复制编辑工具

1. 在 `core/ops/attributes.py` 实现 `_operation(target, params)` 并 `register`，列出允许参数。`target` 已是安全层复制出的工作副本，函数只能修改它。
2. 在相应 `tools/` 模块新增继承 `EditTool` 的类，通过 `run_edit(ctx, in_features=..., params=...)` 调用。
3. 注册到 `ALL_TOOLS`，验证输入原件不变、新结果可读、链路连接源版本；涉及记录删除时沿用现有确认要求。

不要调用私有 `_op_edit` 绕过外层 `op_edit`。如果一个 ArcPy 工具修改多个输入，仅复制一个目标还不够，必须先设计完整适配器。

对 `where`，需要修改值的工具必须把过滤真正用于游标或临时图层/表视图，再对该视图计算。`CalculateField` 不接受一个参数时，不能伪造同名参数或仅返回匹配计数。参考现有 `_calculate_fields`，验证未命中行保持原值、零命中不修改数据，并清理自己创建的临时视图。

带附件、关系、规则等数据的复制限制由 `copy_for_edit` 管理。新增算法不能擅自取消这些限制；若要支持新的数据结构，必须单独验证保真并补安全层测试。

## 扩展通用调用

仅需一次未适配工具时，可使用 `run_unadapted_tool` 的理由与输入/输出声明契约，见 [临时调用说明](safety-history.md#未封装工具的临时调用)。该入口复制全部数据集输入并持久化阶段日志，不会把工具加入永久允许列表；范围外的副作用仍须新增专用适配器。需要长期稳定使用或复杂参数时，继续按下述方式封装和验证。

入口为 `core/ops/meta.py`，查询和执行必须继续使用同一套分类：

| 位置 | 用途 |
| --- | --- |
| `SAFE_PRODUCERS` | 经核验的输入不变、单显式输出工具 |
| `EDIT_ADAPTERS` | ArcPy 参数到现有复制编辑操作的映射 |
| `CREATE_ADAPTERS` | 当前目录与名称组合输出的特殊分支 |
| `READ_ADAPTERS` | 当前只读分支 |
| `tool_execution_support` | 搜索和参数查询返回的能力与限制 |

`SAFE_PRODUCERS` 可在核验后加入规范化的 `工具名_工具箱` 小写名称；其他集合或映射还必须与对应执行分支、输出和结果格式匹配，不能只加名字。新类型需要实现专门分支，并同步 `tool_execution_support` 的 `mode`、`constraints` 及参数限制。

验收至少包括：参数元数据和本地化类型能正确解析；源数据不变；输出暂存后发布；输出与输入同路径被拒绝；重复输出需要明确覆盖；真实输入/输出记入链路；未知工具和不支持的参数仍拒绝执行。派生输出不是新成果，不能据此允许修改输入。

搜索的 `total_matched` 是目录数，`classified_matched` 是其中有适配器的数量；`execution.allowed=true` 只表示可以进入适配器校验。不得把这些字段改写为“全部经过验证”或“任意 ArcPy 工具可执行”。

## 参数、结果与 AI 使用协议

- MCP 只判定执行契约和数据安全，不以面积变化、融合数量或零命中判定业务成败；不要新增 `validation_status` 一类业务验收状态。需要业务核验时由 AI 选择只读统计/读取工具；参数单位、像元中心规则、简化等算法语义必须明确说明。
- 项目/会话可指定 `output_workspace` 并关闭 `auto_create_gdb`。固定产出工具复用 resolver，通用调用在 worker 解析元数据；裸名称进入默认 GDB，显式路径不改写。文件导出必须使用库外的显式路径，不能把 CSV/Excel/Shapefile 当 GDB 内部文件。
- 方法签名使用明确的类型、默认值和中文 docstring；说明 SQL、单位、坐标系、空输入以及格式限制。
- `operation` 是 worker 操作名，可能与 MCP 的 `name` 不同，例如 `update_features` 对应 `update`；确认两端一致。
- 需要较长预算可设置工具类 `timeout`，或配置 `tool_timeouts`。当前写操作硬上限为预算两倍；提高超时不能替代 job 查询或修复锁问题。
- 处理中返回的 `job_id`、`pending` 和错误状态不能丢弃，禁止提前包装成完成成功。
- 最终 `output` / `outputs` 才是后续输入。源路径和调用参数保持逻辑身份，不能伪装成已被原地修改。
- 未指定名称的复制编辑成果由安全层命名为 `原名_操作名`，重名加数字序号；通用编辑适配器使用映射后的逻辑操作名。不要在算法里生成随机正式输出名；`version_path` 必须在工作区锁内调用。用户显式指定的输出名不变，历史文件不迁移。
- 普通面积字段不会因几何改变自动重算；涉及消除、融合等操作时说明影响，不伪造新面积。
- CSV 产出参考 `core/ops/tabular.py` 的 `table_output`，避免绕过已有 sidecar 管理。字段类型保真优先使用 GDB 表。
- 不新建自定义日志替代 `History`；失败任务保留错误，成功输入/输出进入链路。外部手工修改不在自动记录范围内。
- 默认查询字段仅回 name/type；`schema_detail=true` 获取完整 schema。链路默认精简分页，`detail=true` 可取完整结果，完整审计仍保存于 SQLite，不随精简而丢失。`job_result(wait_seconds=...)` 的等待最多 5 秒，不占 worker 锁，不重放任务。
- `table_output` 将游标引用限制在独立导出函数中，释放后清理缓存及临时库。清理失败保留标记，后续写请求在获取临时库锁后重试；不得删除未标记目录或通过清理工作区移除成功成果。

## 验证与交付

在仓库根目录执行基础检查（PowerShell）：

```powershell
.venv/Scripts/python.exe -X utf8 -m compileall -q src tests
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_safety.py
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_execution_contracts.py
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_unadapted.py
.venv/Scripts/python.exe -X utf8 tests/test_project.py
.venv/Scripts/python.exe -X utf8 tests/test_resolver.py
```

`test_project.py` 和 `test_resolver.py` 是脚本检查，不能用 unittest 的发现数量代替它们的执行。若运行环境限制系统临时目录，可把测试临时目录指定到独立可写的测试位置；不能因此改写真实项目数据。

无 ArcPy 的真实 stdio 协议检查：`.venv/Scripts/python.exe -X utf8 tests/contracts_mcp_check.py`，会自行启动服务并仅使用临时项目，验证新参数 schema 与查询返回。

具备 ArcGIS Pro、有效 ArcPy 环境和相应工具许可时执行：

```powershell
.venv/Scripts/python.exe -X utf8 tests/safety_arcpy_check.py
```

该脚本自行启动 worker，在仓库下建立临时项目。需要通过 MCP 验证参数 schema 和客户端任务轮询时，重启测试服务，再按 `tests/tools_check.py`、`tests/meta_check.py` 的现有方式增加针对性测试；它们需要服务运行和相应样例数据。使用单独测试实例，避免连接正在处理业务的服务。

按新增行为选择断言，不必为无关改动重跑所有真实 GIS 流程：

| 行为 | 必须观察的证据 |
| --- | --- |
| 产出 | 实际记录、几何或栅格值符合预期；源内容不变 |
| 条件编辑 | 命中行变化、未命中行不变、零命中结果正确 |
| 覆盖 | 请求路径是新结果，归档路径仍可读旧内容 |
| 任务 | 提交得到 ID，最终结果可查；状态查询不等待繁忙 worker |
| 链路 | 新成果能回溯全部输入、参数和中间路径 |
| 失败 | 参数/锁/执行错误不会破坏源数据，也不宣告完成 |
| 发布/审计基础设施变更 | 注入改名与审计提交前后中断，检查恢复和不确定状态 |

每次扩展提交中写清：ArcPy 名称及版本、开放参数、修改与保留语义、测试命令及结果、未验证范围。没有 ArcPy 时可以交付待验证实现，但不能称其已经可靠支持；新增通用执行分类应等待真实验证。

## 当前限制也要继承

单个 worker 串行执行，协作锁不能阻止 ArcGIS Pro 或外部程序占用数据；不同账户/机器共享写入还需统一锁目录和路径并验证文件锁。不要用“读操作不修改数据”推断它不持有 ArcGIS schema lock。

文件发布与审计数据库之间存在中断窗口，当前 `published` / `retired` 状态与未提交审计的自动对账尚未完善；不得用新工具掩盖这一限制，也不得通过自动重试写操作补救未知结果。

本指南不要求实现回退工具或自动删除历史版本。保留中间成果、让 AI 能解释每一步，才是当前数据策略。
