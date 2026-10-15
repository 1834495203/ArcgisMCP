# arcgis-mcp

用本机 arcpy 读 ArcGIS 数据（gdb / shapefile）的 MCP 工具服务，供 Codex / Claude Code / ZCode 等 AI 客户端调用。

写操作现采用版本保留模式：原地编辑先复制再操作，每一步成功输出保留；显式 overwrite=true 会归档旧目标后替换指定路径。
后续步骤必须使用返回的 `output` / `out_features` 路径；长任务返回 `job_id` 后用
`job_status` / `job_result` 查询。`lineage` / `explain` 可查询整个数据集的加工历史。
详细语义、锁、超时和通用工具限制见 [数据安全与操作历史](docs/safety-history.md)。

## 用法就两步

```
1. AI 把项目根目录告诉服务端（一次就够，服务端会记住）
     use_project(folder="D:\\projects\\西部规划")

2. 之后所有参数都用 ./ 相对路径，服务端自己拼上前缀
     read_gdb_data(gdb="./data/City.gdb", dataset="cities")
     read_shp_data(shp="./矢量/roads.shp")
```

只写名字也行，服务端会在项目根目录下按名字找：

```
read_gdb_data(gdb="City.gdb", dataset="cities")     # 等价于 ./ 找到的那个 City.gdb
read_gdb_data(gdb="./data/City.gdb/cities")         # 要素类写在路径里，dataset 都不用填
read_gdb_data(gdb="./data/City.gdb")                # 不填 dataset：它会把库里有哪些数据列出来
```

不需要写别名，不需要在客户端改配置，不需要填磁盘绝对路径。

`use_project` 只是「把根目录告诉服务端」这一个动作；这一步也可以由客户端自动完成
（stdio 模式看工作目录、HTTP 模式看 `X-Project-Root` 请求头），见文末「可选」。

### 这一步是强制的（服务端会拦）

不只是写在工具说明里靠 AI 自觉：**没确定项目根目录时，需要项目的工具会被直接拒绝**。
`read_gdb_data` / `read_shp_data` / `list_gdb_datasets` / `list_shp_datasets`
这四个工具会返回一个标准 MCP 错误（`is_error=true`），内容是：

```
还不能执行 read_gdb_data：服务端不知道你在哪个项目目录里，因此无法解析数据路径。

请先调用一次：
    use_project(folder="<项目根目录的绝对路径>")
...
```

模型收到的是错误而不是结果，所以会停下来按提示先调 `use_project`。
（纯发现类的 `list_datasources` / `arcgis_status` / `use_project` 不受限制，
否则 AI 连「为什么不能干活」都查不到。）

例外情况：
- 客户端已经通过 stdio 工作目录或 `X-Project-Root` 请求头告诉了服务端 → 自动放行，AI 不用调；
- `use_project` 传了不存在的目录 → 同样是硬错误，提示要用绝对路径。

想关掉这个约束（例如只用全局 `workspaces` 里的共享数据、完全不使用项目概念），
在 `config/datasources.json` 里设 `"require_project": false`。默认是 `true`。

## 服务端怎么找到数据

设置项目根目录后，服务端做三件事：

1. 把**项目根目录本身作为查找基准**（所以 `./data/City.gdb` 和 `City.gdb` 都能找到）；
2. 扫描该目录下的 `*.gdb` 和含 shapefile 的目录，列出来给你看（`.venv`、`node_modules` 等自动跳过）；
3. 如果项目里有 `<项目>/.arcgis-mcp/datasources.json`，读它（给固定别名用，**可选**）。

## 架构：为什么是两个 Python

ArcGIS Pro 3.x 的 `arcpy` 是 **cp39 的二进制扩展**，只能在 ArcGIS Pro 自带的 Python 3.9 里 import；
官方 `mcp` SDK 需要 Python ≥ 3.10。两者装不进同一个解释器，所以拆成两个进程：

```
  AI 客户端 (Codex / Claude / ZCode)
        │  MCP over Streamable HTTP  http://127.0.0.1:8765/mcp
        ▼
  MCP 服务端  .venv  Python 3.12 + mcp SDK
     ├─ tools/     工具实现：use_project / read_gdb_data / intersect / dissolve …
     │             每个工具一个类，继承 core/tool.py 的 Tool 基类
     ├─ core/      基础设施：配置、项目作用域、路径解析、日志、工具基类
     └─ server.py  只做组装（App 容器 + 注册工具与中间件）
        │      负责：项目根目录 → 完整路径、分页、错误提示、日志
        │  stdin/stdout JSON 行协议（常驻，整个服务生命周期只启动一次）
        ▼
  arcpy 工作进程  ArcGIS Pro Python 3.9 + arcpy 3.1.5
     core/arcpy_worker.py —— 只用标准库，按文件路径直接启动
              负责：ListFeatureClasses / SearchCursor / Describe …
```

工作进程在服务启动时后台预热，之后所有调用复用同一个（arcpy 的 import 很贵，不能每次调用重启）。
注意 worker 是**按文件路径**启动的（不是 `-m`）：`-m` 会先 import 包，而 `core/__init__.py` 会连带
import mcp SDK，但 arcpy 那个 Python 3.9 里没有 mcp，会一启动就崩。

## 目录结构

```
arcgis-mcp/
├─ config/datasources.json         全局配置：跨项目共享数据 + 各种开关（默认留空）
├─ .arcgis-mcp/datasources.json    本仓库自己的项目级配置（示例）
├─ src/arcgis_mcp/
│  ├─ core/                        ★ 基础设施（与具体工具无关）
│  │  ├─ config.py                 配置加载与校验
│  │  ├─ project.py                项目作用域：项目根目录识别、自动扫描、分层合并
│  │  ├─ resolver.py               名字 / ./ 相对路径 → 完整路径（含“找不到时给候选”）
│  │  ├─ worker.py                 arcpy 子进程的管理（复用、超时、崩溃重启、日志转发）
│  │  ├─ arcpy_worker.py           子进程入口：协议 + 读取类操作 + 分派到 ops/
│  │  ├─ ops/                      ★ arcpy 侧操作实现，**按功能域分模块**
│  │  │  ├─ common.py              共用：错误类型、值序列化、路径/字段/输出容器
│  │  │  ├─ vector.py              叠加 + 邻域（intersect/union/erase/clip/buffer/…）
│  │  │  ├─ management.py          数据管理（copy/merge/project/repair/dissolve/…）
│  │  │  ├─ cartography.py         制图综合（smooth/simplify/densify）
│  │  │  ├─ raster.py              栅格（转面/转点/重分类/掩膜/重采样/分区统计）
│  │  │  ├─ conversion.py          格式转换（Excel/CSV/JSON/坐标表转点）
│  │  │  ├─ statistics.py          统计（汇总/频数）
│  │  │  └─ attributes.py          字段与几何编辑（原地：加字段/赋值/修复几何/删除）
│  │  ├─ tool.py                   ★ Tool 抽象基类：项目门禁、arcpy 调用、日志、错误包装
│  │  ├─ middleware.py             统一记录每次工具调用（名字/参数/耗时/可疑参数名）
│  │  ├─ logs.py                   统一日志（**不能叫 logging.py**，见文件里的说明）
│  │  └─ mcp_compat.py             mcp SDK 版本兼容导入（唯一允许 try/except import 的地方）
│  ├─ tools/                       ★ 工具实现（每个工具一个类，继承 Tool）
│  │  ├─ __init__.py               ALL_TOOLS 注册表 + “怎么加工具”说明
│  │  ├─ base.py                   ★ GeoProcessTool（产出新数据集）/ EditTool（复制后编辑）
│  │  ├─ workspace.py              use_project / list_datasources / arcgis_status
│  │  ├─ gdb.py                    list_gdb_datasets / read_gdb_data
│  │  ├─ shp.py                    list_shp_datasets / read_shp_data
│  │  ├─ analysis.py               叠加 + 邻域 + 统计（13 个）
│  │  ├─ management.py             数据管理 + 字段与几何编辑（25 个）
│  │  ├─ cartography.py            平滑 / 抽稀 / 加密
│  │  ├─ raster.py                 栅格（10 个，含 describe_data）
│  │  └─ conversion.py             格式转换（7 个）
│  ├─ server.py                    组装：App 容器 + build_server（只有装配代码，无全局变量）
│  └─ __main__.py                  CLI：http / stdio / --check / --init-project
├─ scripts/                        一键脚本：run-http / run-stdio / check / install
├─ tests/                          单元 + 端到端测试
└─ data/                           示例数据
   ├─ Demo.gdb                     cities / base·rivers / districts（含多部件与小面）/ notes
   ├─ shp/                         roads.shp、zones.shp、soils.shp（含寒冻土）、points/poi.shp
   └─ rasters/landcover.tif        6x6 栅格，值 0/1/2/3（单格约 11 亩、3×3 块约 100 亩）
```

分层的原则：**core 不知道有哪些工具，tools 只通过基类提供的能力干活**。
`tools/*.py` 里看不到一行 MCP 协议代码，也看不到 arcpy 子进程的管理细节。

## 日志

三层日志开箱即用，写工具时不用自己加样板：

```
[arcgis-mcp] 22:50:55 INFO    arcgis_mcp.call: read_gdb_data({"gdb": "./data/Demo.gdb", "dataset": "cities"})
[arcgis-mcp] 22:50:55 INFO    arcgis_mcp.tools.read_gdb_data: 读取 D:\...\Demo.gdb / cities（limit=5, geometry=none）
[arcgis-mcp] 22:50:55 INFO    arcgis_mcp.tools.read_gdb_data: arcpy.read 完成（0.39s）{"path": "D:\\...\\cities", "returned": 5, …}
[arcgis-mcp] 22:50:55 INFO    arcgis_mcp.call: read_gdb_data -> 完成 0.42s
[arcgis-mcp] 22:50:55 WARNING arcgis_mcp.call: use_project !! 失败 0.00s：ToolError: 项目目录不存在：…
```

| 层 | 谁写的 | 记的是什么 |
| --- | --- | --- |
| `arcgis_mcp.call` | `core/middleware.py`（自动） | 每次工具调用的名字、参数摘要、耗时、失败原因 |
| `arcgis_mcp.tools.<工具名>` | 基类的 `self.trace()` / `self.arcpy()` | 工具自己的进度、arcpy 调用的入参与耗时 |
| `arcgis_mcp.core.worker` | 子进程管理 | arcpy 进程的启动/退出、arcpy 自己的 stderr 输出 |

工具里调 `self.trace("...", ctx)` 除了写服务端日志，还会**推给 MCP 客户端**（AI 能看到进度，
实测 `read_gdb_data` 会收到 `[read_gdb_data] 读取 ...`）。推送失败只降级为不推送，不影响工具执行。

日志级别用环境变量调：`ARCGIS_MCP_LOG_LEVEL=DEBUG`（会打印 arcpy 入参等细节）。
所有日志都写到 stderr —— stdout 在 stdio 模式下是 MCP 协议通道，绝不能污染。

## 完整工作流示例（一条真实流程的等价复现）

下面是「栅格转面 → 消除 15 亩图斑两次 → 平滑两次 → 从 B 取寒冻土相交 → 转单部件 → 消除 → 赋值 →
更新 B → 融合」的拆解。这条流程**已经在样例数据上端到端跑通**
（`tests/workflow_check.py`，41 项断言全过）。

```
use_project(folder="D:\\projects\\西部规划")

# 1) 栅格转面（这一步输出的是 shapefile，没有面积字段）
raster_to_polygon(in_raster="./rasters/landcover.tif", out_features="./out/A0.shp",
                  create_multipart=False)

# 2) 算面积（亩）→ 消除 15 亩以下图斑 → 再消除一遍（第二遍为空操作也不报错）
add_field(in_features="./out/A0.shp", field="AREA_MU", field_type="DOUBLE")
calculate_fields(in_features="./out/A0.shp",
                 assignments={"AREA_MU": "!shape.geodesicArea@SQUAREMETERS! / 666.6667"})
eliminate(in_features="./out/A0.shp", out_features="./out/A1.shp", where="AREA_MU < 15")
eliminate(in_features="./out/A1.shp", out_features="./out/A2.shp", where="AREA_MU < 15")

# 3) 平滑两次 → A
smooth(in_features="./out/A2.shp", out_features="./out/A_s1.shp", tolerance=0.01)
smooth(in_features="./out/A_s1.shp", out_features="./out/A.shp", tolerance=0.01)

# 4) 把 B 里的寒冻土导出
select(in_features="./data/shp/soils.shp", out_features="./out/寒冻土.shp",
       where="TYPE = '寒冻土'")

# 5) 寒冻土 ∩ A → C（相交会把图斑打碎，C 里带着 A 的 gridcode）
intersect(in_features=["./out/寒冻土.shp", "./out/A.shp"], out_features="./out/C.shp")

# 6) 转单部件
multipart_to_singlepart(in_features="./out/C.shp", out_features="./out/C_s.shp")

# 7) 面积变了要重算，再消除 15 亩以下两次 → D
calculate_fields(in_features="./out/C_s.shp",
                 assignments={"AREA_MU": "!shape.geodesicArea@SQUAREMETERS! / 666.6667"})
eliminate(in_features="./out/C_s.shp", out_features="./out/D1.shp", where="AREA_MU < 15")
eliminate(in_features="./out/D1.shp", out_features="./out/D.shp", where="AREA_MU < 15")

# 8) 给 D 赋值：gridcode 为 0 的图斑，TL/YL/TS 分别赋成 草毡土/薄草毡土/砾砂薄草毡土
calculate_fields(in_features="./out/D.shp",
                 assignments={"TL": "'草毡土'", "YL": "'薄草毡土'", "TS": "'砾砂薄草毡土'"},
                 where="gridcode = 0")

# 9) 用 D 去更新 B（输出到新数据集，原 B 不动）
update(in_features="./data/shp/soils.shp", update_features="./out/D.shp",
       out_features="./out/B_updated.shp")

# 10) 按 TL YL TS TZ 融合
dissolve(in_features="./out/B_updated.shp", out_features="./out/E.shp",
         dissolve_field=["TL", "YL", "TS", "TZ"])
```

在样例数据上，这条流程的结果是：A0 4 个图斑 →（消除 <15 亩）3 个 → 平滑后仍 3 个 →
相交打碎成 9 个 → 重算面积再消除 → 3 个 → 赋值的图斑回读出 `TL=草毡土` →
更新 B 得 6 个 → 按四个字段融合成 2 个。

## 加一个新工具 = 三步

1. 在 `tools/` 里对应的功能域模块中写一个类。基类按「是否产出新数据集」选：

   - **产出新数据集** → 继承 `base.GeoProcessTool`（buffer / union / project 这类）
   - **复制目标后编辑新版本** → 继承 `base.EditTool`（add_field / repair_geometry 这类）
   - 只读的工具 → 继承 `core.tool.Tool`

```python
class MyTool(GeoProcessTool):
    name = "my_tool"            # = MCP 工具名 = 下面那个方法名（基类会检查）
    operation = "my_operation"  # 对应 core/ops 里注册的操作名
    needs_project = True        # 需要项目根目录就设这一处：
                                #   ① 工具说明里的【前置条件】由基类自动加上
                                #   ② self.view(ctx) 会自动做门禁检查

    def my_tool(self, ctx: Context, in_features: str, out_features: str,
                limit: int = 10, overwrite: bool = False) -> str:
        """工具说明（原样发给 AI，写清楚参数和返回值。前置条件不用写，基类会拼）。"""
        self.trace(f"处理 {in_features}", ctx)     # 记日志（同时推给客户端）
        return self.run_geoprocess(ctx, in_features=[in_features],
                                   out_features=out_features,
                                   params={"limit": limit}, overwrite=overwrite)
```

2. 把类加进 `tools/__init__.py` 的 `ALL_TOOLS`（顺序 = AI 看到的工具列表顺序）。
3. arcpy 侧还没有这个操作时，在 `core/ops/<功能域>.py` 里实现并 `register(...)`。

   `core/ops/*.py` 运行在 ArcGIS Pro 的 Python 3.9 里，**只能依赖标准库和 arcpy**，
   不能 import 包内其他模块（会连带 import 需要 3.10+ 的 mcp SDK，worker 直接崩）。
   操作要么写进 `GEOPROCESSING`（in → out），要么写进 `EDITING`（原地改）。
   输出容器创建、覆盖保护、同路径拦截、输出摘要、字段名提示都由 worker 统一处理，不用重复写。

**通用提示只写一处。** 工具说明 = 基类按 `needs_project` 自动加的【前置条件】+ 类属性 `notice`
（该工具自己的通用提示）+ 方法 docstring。所以方法 docstring 里只写「这个工具干什么、参数怎么填」，
不要重复讲前置条件。改提示文案只需要改 `core/tool.py` 里的 `PROJECT_PRECONDITION`。

基类已经替你处理好的事：项目门禁（`needs_project` 一处声明）、`./` 相对路径解析、
arcpy 调用（含日志与超时）、错误包装（`self.error()` 给可自纠的业务错误、
`self.require_project(view)` 给需要手动判断的硬前置条件）、分页提示（`self.pagination_hint()`）、
响应体过大自动削减（`self.shrink()`）。

约定（写错会在启动时直接报错，不会静默失效）：`name` 必须等于公开方法名；
方法签名的类型注解就是给 AI 的入参 schema，所以别用 `**kwargs`；
`ctx: Context` 由 SDK 注入、不占 schema 参数，放第一位。

## 通用 arcpy 能力（固定工具不够用时的出口）★

固定工具是「常用操作的便捷封装」——参数校验、中文友好报错、「按 15 亩消除」这类经验都固化在里面。
本机 ArcPy 目录可包含上千个工具（曾在 ArcGIS Pro 3.1.5 上枚举到 1806 个，随安装和扩展变化），这不是可执行或已验证数量。三个通用工具区分搜索、参数查询和受限执行：

| 工具 | 作用 |
| --- | --- |
| `list_arcpy_tools` | **搜**：按英文关键词或工具箱找工具，返回名字、语法行及 `execution` 安全适配状态 |
| `describe_arcpy_tool` | **查**：任意工具的参数、安全适配状态和执行限制；可查询不代表可执行 |
| `run_arcpy_tool` | **调**：按参数名调用已完成安全分类的 arcpy 工具（路径解析、覆盖保护、输出摘要都有） |

优先使用固定封装。不足时按下面三步操作：先确认 `execution.allowed=true`，再检查 `constraints`；参数名以 describe 的结果为准。安全适配不代表所有参数组合均已实测，坐标系、单位、许可证和结果仍需核验。

```
list_arcpy_tools(keyword="Tessellation")
  → GenerateTessellation_management，execution: {allowed: true, mode: "produce_publish", ...}

describe_arcpy_tool(tool="GenerateTessellation")
  → parameters: Output_Feature_Class(Output,必填) / Extent(Input,必填) / Shape_Type(可选值:六边形,方形,三角形,菱形…) / Size / Spatial_Reference

run_arcpy_tool(tool="GenerateTessellation",
               parameters={"Output_Feature_Class": "./out/格网.shp",
                           "Extent": "./data/Demo.gdb/districts",
                           "Shape_Type": "六边形", "Size": 0.5})
  → job_id: ...（用 job_status / job_result 获取最终 outputs）
```

搜索和参数查询覆盖本机全部 ArcPy 工具；执行入口只允许已完成安全分类的工具。
`total_matched` 是匹配目录数，`classified_matched` 是其中已安全分类数，两者都在分页截断前统计。
每项 `execution` 提供 `allowed`、`mode`、`constraints`、`validation`；编辑适配器另列 `supported_parameters`。
`allowed=true` 表示已有适配器，实际参数仍须通过校验；`allowed=false` 的工具即使传 `confirm=true` 也不能执行。
Pairwise 系列、GeneratePointsAlongLines 等常用产出工具已支持；其他工具需要添加安全适配器。
（实测跑通的有：`GenerateTessellation` 生成 1134 个六边形格网、`PairwiseDissolve_analysis`、
`GeneratePointsAlongLines`、`management.CreateFeatureclass` 写进新建的 gdb。）

`run_arcpy_tool` 的护栏和固定工具一致：
- 路径参数写 `./` 相对路径即可（服务端按项目根目录解析；输出目录/缺失的 .gdb 会自动创建并在
  `container_notes` 里说明）；
- 输出已存在时默认报错，要归档旧版并替换目标得传 `overwrite=true`；
- 输出和输入是同一个数据集时直接拦下；
- 编辑类工具通过安全适配器先复制再操作；DeleteFeatures/DeleteRows 在副本上删除记录，Delete 归档原件并移除原路径，均要求 confirm=true；其他未分类工具仍被拒绝；
- **写到项目目录和 search_roots 之外**的路径也要求 `confirm=true`（把这些目录加进 `search_roots` 就不用每次确认）。

### 任意代码入口：`run_arcpy_code`

审计保留模式下不执行任意 Python 代码，避免绕过复制、写锁和操作历史。
默认不注册；即使旧配置设置了 `allow_arcpy_code=true`，也只保留兼容入口并返回说明。
需要更多操作时，使用已完成安全分类的 `run_arcpy_tool`，或添加安全适配器。

## 快速开始

### 1. 安装

```bat
scripts\install.bat
```

建 `.venv`、装 `mcp`（走清华镜像）、把 `src` 注册进虚拟环境。

### 2. 启动服务

```bat
scripts\run-http.bat                                  :: http://127.0.0.1:8765/mcp
scripts\run-http.bat --project D:\projects\西部规划      :: 也可以直接把项目根目录固定死
```

`scripts\check.bat` 自检：打印配置、背景预热 arcpy 的结果等。

### 3. 接入客户端

**方式一：什么都不配（推荐）** —— AI 自己调 `use_project`。因为 AI 在项目目录里跑，
它 `pwd` 一下就知道路径，直接报给服务端即可。

**方式二：让客户端自动报（更省事，但要配一行）** —— 见下面「可选」。

### 4. 可选：给项目生成固定别名

不生成也能用（按名字/相对路径都能读）。想让名字更稳定可读：

```bat
.venv\Scripts\python.exe -m arcgis_mcp --init-project D:\projects\西部规划
```

扫描目录、写 `<项目>/.arcgis-mcp/datasources.json`（里面的相对路径按项目目录解析，
可以直接提交到仓库），并打印各客户端的接入片段。

## 工具（共 65 个，按功能域分组）

「产出新数据集」的工具都接受 `overwrite`（默认 false，不覆盖已有结果）；
编辑类工具先复制到新版本再修改，输入原件保留；后续调用必须使用返回路径。

### 项目与探索

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| **`use_project`** | **把项目根目录告诉服务端（通常第一个调）** | `folder` |
| `list_datasources` | 看当前项目扫到了哪些数据源 | 无 |
| **`describe_data`** | **看任意数据集/栅格的元信息（类型、字段、坐标系、像元大小…）** | `dataset` |
| `arcgis_status` | arcpy 环境自检 | 无 |

### 读取

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| `list_gdb_datasets` | 列出 gdb 里所有要素类 / 表 | `gdb` |
| **`read_gdb_data`** | **读 gdb 里的要素类 / 表** | `gdb`、`dataset`、`where`、`fields`、`limit`、`offset`、`geometry`、`order_by` |
| `list_shp_datasets` | 列出目录下的 shapefile | `folder`、`recursive` |
| **`read_shp_data`** | **读 shapefile** | `shp`、`folder`、`where`、`fields`、`limit`、`offset`、`geometry`、`order_by` |

### 分析（叠加 / 邻域 / 统计）

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| `intersect` | 相交（求公共部分，合并属性） | `in_features`（≥2）、`out_features`、`join_attributes`、`output_type`、`cluster_tolerance` |
| `union` | 联合（求并集，保留全部范围） | `in_features`（≥2）、`out_features`、`join_attributes`、`gaps` |
| `erase` | 擦除（挖掉另一图层覆盖的部分） | `in_features`、`erase_features`、`out_features` |
| `clip` | 裁剪（只留另一图层范围内的部分） | `in_features`、`clip_features`、`out_features` |
| `identity` | 标识（叠加并保留输入的全部范围） | `in_features`、`identity_features`、`out_features`、`join_attributes`、`relationship` |
| `symmetrical_difference` | 对称差（两边不重叠的部分） | `in_features`、`update_features`、`out_features` |
| `buffer` | 缓冲区 | `in_features`、`out_features`、`distance`、`dissolve_option`、`line_side`、`line_end_type`、`method` |
| `spatial_join` | 空间连接（按空间关系连属性） | `target_features`、`join_features`、`out_features`、`match_option`、`join_operation`、`join_type`、`search_radius` |
| `near` | 近邻分析（最近要素 + 距离） | `in_features`、`near_features`、`out_features`、`search_radius`、`location`、`angle`、`method` |
| `select` | 按属性筛选导出子集 | `in_features`、`out_features`、`where` |
| `select_by_location` | 按位置筛选导出 | `in_features`、`select_features`、`out_features`、`relation`、`search_distance`、`invert` |
| `summary_statistics` | 汇总统计（可分组） | `in_features`、`out_features`、`statistics_fields`、`case_field` |
| `frequency` | 频数统计 | `in_features`、`out_features`、`frequency_field`、`summary_fields` |

### 数据管理

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| `copy_features` | 复制数据集（也能 shp ↔ gdb 互转） | `in_features`、`out_features` |
| `merge` | 合并多个数据集（上下拼接） | `in_features`（≥2）、`out_features`、`add_source` |
| `append` | 追加到已有数据集（**复制目标后追加**） | `in_features`、`target`、`schema_type`、`expression`、`match_fields` |
| `project` | 投影转换（真正换算坐标） | `in_features`、`out_features`、`spatial_reference`、`transform_method` |
| `define_projection` | 定义投影（只声明坐标系） | `in_features`、`out_features`、`spatial_reference` |
| `check_geometry` | 几何检查（输出问题清单表） | `in_features`、`out_features`、`validation_method` |
| `create_gdb` | 创建文件地理数据库 | `out_features`（`.gdb`）、`version` |
| `create_feature_class` | 创建空要素类 / shapefile | `out_features`、`geometry_type`、`template`、`spatial_reference` |
| `create_table` | 创建空表 | `out_features`、`template` |
| `feature_to_point` | 要素转点（重心 / 内部点） | `in_features`、`out_features`、`inside` |
| `feature_to_line` | 要素转线（面边界转线） | `in_features`、`out_features`、`attributes`、`cluster_tolerance` |
| `minimum_bounding_geometry` | 最小包围几何（矩形/凸包/圆/包络） | `in_features`、`out_features`、`geometry_type`、`group_option`、`group_field`、`add_geometry` |
| `sort` | 按字段排序 | `in_features`、`out_features`、`sort_field` |
| `multipart_to_singlepart` | 多部件转单部件 | `in_features`、`out_features` |
| `dissolve` | 融合（按字段合并） | `in_features`、`out_features`、`dissolve_field`、`statistics_fields`、`multi_part` |
| `eliminate` | 消除（把小面并进邻面） | `in_features`、`out_features`、`where`、`merge_with` |
| `update_features` | 更新（用一份面更新另一份面，输出新数据集） | `in_features`、`update_features`、`out_features`、`keep_borders`、`cluster_tolerance` |

### 字段与几何编辑（**复制后生成新版本**）

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| `add_field` | 加字段 | `in_features`、`field`、`field_type`、`field_length`、`field_alias`、`field_is_nullable` |
| `calculate_fields` | 批量赋值 / 字段计算（支持 `where`） | `in_features`、`assignments`、`where`、`field_type`、`expression_type`、`code_block` |
| `delete_field` | 在副本中删字段（保留原件） | `in_features`、`field` |
| `alter_field` | 改字段名 / 别名 / 类型（**仅地理数据库**） | `in_features`、`field`、`new_name`、`new_alias`、`field_type`、`field_length` |
| `add_xy_coordinates` | 给点要素加 `POINT_X` / `POINT_Y` | `in_features` |
| `add_geometry_attributes` | 加几何属性字段（面积/长度/周长/中心点…） | `in_features`、`geometry_properties`、`area_unit`、`length_unit` |
| `repair_geometry` | 修复几何（自相交、空几何…） | `in_features`、`delete_null`、`validation_method` |
| `delete_dataset` | 删除数据集（不可逆，需 `confirm=true`） | `in_features`、`confirm` |

### 制图综合

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| `smooth` | 平滑线/面的尖锐拐角 | `in_features`、`out_features`、`tolerance`、`algorithm` |
| `simplify` | 抽稀（去掉多余顶点、保形状） | `in_features`、`out_features`、`tolerance`、`algorithm` |
| `densify` | 加密顶点 | `in_features`、`out_features`、`method`、`distance`、`max_deviation`、`max_angle` |

### 栅格

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| `raster_to_polygon` | 栅格转面 | `in_raster`、`out_features`、`simplify`、`raster_field`、`create_multipart`、`max_vertices` |
| `raster_to_point` | 栅格转点 | `in_raster`、`out_features`、`raster_field` |
| `polygon_to_raster` | 矢量转栅格 | `in_features`、`out_features`、`value_field`、`cell_size`、`cell_assignment` |
| `reclassify` | 栅格重分类（`remap="0 1; 1 2"`） | `in_raster`、`out_features`、`remap`、`reclass_field`、`missing_values` |
| `extract_by_mask` | 按掩膜提取 | `in_raster`、`mask`、`out_features`、`extraction_area` |
| `copy_raster` | 复制栅格（换格式/压缩） | `in_raster`、`out_features`、`pixel_type`、`format`、`nodata_value` |
| `resample` | 重采样（改分辨率） | `in_raster`、`out_features`、`cell_size`、`resampling_type` |
| `project_raster` | 栅格投影转换 | `in_raster`、`out_features`、`spatial_reference`、`resampling_type`、`cell_size` |
| `zonal_statistics` | 分区统计（矢量分区 + 栅格值 → 表） | `in_features`、`in_value_raster`、`out_features`、`zone_field`、`statistics_type` |

### 格式转换

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| `table_to_excel` | 要素/表导出 Excel | `in_features`、`out_features`（`.xlsx`）、`use_field_alias` |
| `excel_to_table` | Excel 转表 | `in_excel`、`out_features`、`sheet`、`field_names_row`、`cell_range` |
| `export_table` | 导出 CSV / TXT / DBF | `in_features`、`out_features`、`where` |
| `features_to_json` | 要素导出 JSON / GeoJSON | `in_features`、`out_features`、`geojson`、`include_z`、`include_m`、`to_wgs84` |
| `json_to_features` | JSON / GeoJSON 转要素 | `in_json`、`out_features`、`geometry_type` |
| `xy_table_to_point` | 坐标表转点要素 | `in_features`、`out_features`、`x_field`、`y_field`、`z_field`、`coordinate_system` |
| `raster_to_other_format` | 栅格批量转格式（输出到目录） | `in_rasters`、`out_folder`、`raster_format` |

### 所有写入类工具共用的约定

- **输入输出都写 `./` 相对路径**（相对项目根目录），例如
  `in_features="./data/Demo.gdb/districts"`、`out_features="./out/融合.shp"`；也接受绝对路径。
- **输出目录不存在会自动建**；输出写成 `./out/新成果.gdb/要素类名` 时，缺失的 .gdb 也会自动创建
  （返回值里的 `container_note` 会说明）。
- **输出已存在时默认报错、不覆盖**，归档旧版后替换目标需显式 `overwrite=true`（旧成果保留）。
- **输出不能和输入是同一个数据集**，服务端会直接拦下（那样会把原始数据删掉再重算）。
  编辑属性或几何使用 `add_field` / `calculate_fields` / `repair_geometry`，均先复制再修改并返回新版本。
- 返回值里带 `feature_count` / `record_count` / `shape_type` / `spatial_reference` / `fields`，
  可以直接跟前一步的输入对比，确认操作真的生效了。
- `update_features` **不会原地改数据**：它是「输入 + 更新要素 → 新数据集」。
- `eliminate` **可以安全重复执行**：`where` 没选中任何要素时（例如「消除两次」的第二遍已经没有
  小图斑了）不报错，输出是输入的原样副本，返回值里 `eliminated_count` 为 0 并带 `note`。
- **参数名写错不会被拦**：MCP 对多余参数是静默忽略的，所以拼错的可选参数不会报错、也不生效。
  服务端会在日志里记一条 WARNING（包含该工具的可用参数名），排查「参数没起作用」时看日志。

### 面积阈值怎么给（消除「小于 X 亩的图斑」这类需求）

**1 亩 = 666.6667 平方米**，所以 15 亩 = 10000 平方米。要按亩消除，数据里得先有一个以亩为单位的
面积字段——注意 **`RasterToPolygon` 输出的 shapefile 根本没有面积字段**（只有 `FID/Shape/Id/gridcode`），
gdb 里的要素类才有 `Shape_Area`（单位是数据的面积单位，经纬度数据就是平方度，不能直接跟亩比）：

```
add_field(in_features="./out/A.shp", field="AREA_MU", field_type="DOUBLE")
calculate_fields(in_features="./out/A.shp",
                 assignments={"AREA_MU": "!shape.geodesicArea@SQUAREMETERS! / 666.6667"})
eliminate(in_features="./out/A.shp", out_features="./out/A_clean.shp", where="AREA_MU < 15")
```

`!shape.geodesicArea@SQUAREMETERS!` 会按椭球算真实面积（经纬度数据也能用），
投影数据可以用 `!shape.area@SQUAREMETERS!`。调这个之后面积会变，所以相交 / 拆单部件之后要**重算**。

典型串联（把发现、分析、字段编辑、回读接起来）：

```
use_project(folder="D:\\projects\\西部规划")
read_gdb_data(gdb="./data/Demo.gdb")                        # 先看库里有什么
intersect(in_features=["./data/Demo.gdb/districts", "./data/shp/zones.shp"],
          out_features="./out/相交.shp")                     # 叠加分析
eliminate(in_features="./out/相交.shp", out_features="./out/清理.shp",
          where="Shape_Area < 0.1")                          # 清掉碎面
dissolve(in_features="./out/清理.shp", out_features="./out/融合.shp",
         dissolve_field=["ZONE"])                            # 按字段合并
read_shp_data(shp="./out/融合.shp", limit=0)                 # 回读确认
```


两个读工具的返回结构一致：

```jsonc
{
  "ok": true,
  "path": "D:\\projects\\西部规划\\data\\City.gdb\\cities",  // 实际读的路径，供核对
  "project_root": "D:\\projects\\西部规划",
  "fields": [ { "name": "POP", "type": "Integer", "alias": "人口" } ],
  "total_count": 5,       // 总记录数
  "matched_count": 3,     // where 命中的记录数
  "returned": 3,
  "truncated": true,      // 还有数据没返回 → 用 next_offset 继续翻页
  "next_offset": 3,
  "shape_type": "Point",
  "spatial_reference": { "name": "GCS_WGS_1984", "wkid": 4326 },
  "extent": { "xmin": 101.7, "ymin": 23.15, "xmax": 121.6, "ymax": 40.8 },
  "records": [ { "OBJECTID": 1, "NAME": "Beijing", "POP": 21540000 } ]
}
```

约定：

- **`limit=0`**：只返回表结构 + 计数，不返回记录。先看 schema 再决定怎么查，省 token。
- **`geometry`**：`none`（默认，最快）/ `wkt`（WKT 文本）/ `xy`（坐标值）。
- **分页**：`truncated=true` 时用 `next_offset` 接着读，或加 `where` / `fields` 收窄。
- **字段名**：`where` / `fields` / `order_by` 用真实字段名（`POP`），不是别名（`人口`）。
- **响应体上限**：超过 `defaults.max_response_bytes` 会自动削减记录数并说明原因。

### 典型对话

```
用户：这个项目里有哪些数据？
AI ：use_project(folder="D:\\projects\\西部规划")   → 扫到 city / 矢量
     read_gdb_data(gdb="./data/City.gdb")          → cities, base/rivers, notes

用户：把人口最多的 5 个城市列出来
AI ：read_gdb_data(gdb="./data/City.gdb", dataset="cities",
                   fields=["NAME","POP"], order_by="POP DESC", limit=5)

用户：读一下路网，只要国道
AI ：read_shp_data(shp="./矢量/roads.shp", where="NAME LIKE 'G%'", limit=20)
```

## 可选：让客户端自动把项目目录报给服务端

上面的两步里第 1 步交给客户端做，AI 就更省事。按优先级：

| 来源 | 怎么配 |
| --- | --- |
| `--project D:\proj` | 服务启动参数，把这个实例固定给某个项目 |
| 请求头 `X-Project-Root` | HTTP 客户端按项目配一行头 |
| 环境变量 `ARCGIS_MCP_PROJECT` | 客户端不支持传工作目录时的兜底 |
| 工作目录 cwd | **stdio 模式零配置**：客户端用项目目录启动服务进程，服务端直接读 cwd |
| `default_project` | 全局配置里的兜底 |

### Codex CLI

写进 `~/.codex/config.toml`，或写在项目里的 `<项目>/.codex/config.toml`（只对该项目生效）：

```toml
# 必需：启用 Rust MCP 客户端，否则 streamable-http 的服务不会被加载
experimental_use_rmcp_client = true

[mcp_servers.arcgis]
url = "http://127.0.0.1:8765/mcp"
# 这一行让服务端自动知道项目目录（配了就不需要 AI 调 use_project）
http_headers = { "X-Project-Root" = "D:/projects/west-plan" }
# Codex 默认工具超时 60 秒，arcpy 偶尔更久，建议放宽
tool_timeout_sec = 300
```

部分新版把开关换成了 `[features] rmcp_client = true`。用 `codex mcp list` 确认注册成功。

### Claude Code

```bash
claude mcp add --transport http arcgis http://127.0.0.1:8765/mcp \
  --header "X-Project-Root: D:/projects/west-plan"
```

### ZCode / 其它 HTTP 客户端

选 HTTP / Streamable HTTP，地址 `http://127.0.0.1:8765/mcp`，加请求头 `X-Project-Root: <项目目录>`。
不能加请求头也没关系：AI 调 `use_project` 就行。

### stdio 模式（零配置）

客户端用项目目录作为工作目录启动服务，服务端自动识别：

```json
{
  "mcpServers": {
    "arcgis": {
      "command": "cmd",
      "args": ["/c", "D:\\programming\\fun\\arcgis-mcp\\scripts\\run-stdio.bat"]
    }
  }
}
```

> **请求头里能写中文路径吗？** HTTP 头只能是 ASCII，中文要用百分号编码，服务端会自动解回：
> `X-Project-Root: D:/projects/%E8%A5%BF%E9%83%A8%E8%A7%84%E5%88%92%E9%A1%B9%E7%9B%AE`。
> `--init-project` 打印的片段已经编码好了；或者直接让 AI 用 `use_project`（走 JSON，UTF-8 安全）。

> HTTP 模式只监听 `127.0.0.1`，MCP SDK 还会校验 Host 头，默认不具备远程访问能力。
> 需要跨机器时请自行加反向代理和认证——**不要直接暴露到公网**，这个服务能读本机任意已配置的数据。

## 加一个新工具

见上面「加一个新工具 = 三步」（`core/tool.py` 的模块文档里也写了同样的说明）。

想深入看的话：`tools/gdb.py` 是最典型的例子（含「只给 gdb 不给 dataset 时列出目录」这种分支）；
需要新 arcpy 操作时，在 `core/arcpy_worker.py` 里加一个 `op_xxx(...)` 并注册进 `OPS`，
然后在工具里 `self.arcpy("xxx", ...)` 调用即可（返回值必须可 JSON 序列化，日期/二进制用 `jsonable()`）。

## 贡献与扩展工具

让 AI 修改本仓库前，先阅读根目录 [AGENTS.md](AGENTS.md)。贡献者可按 [工具扩展指南](docs/extending-tools.md) 接入新工具，其中包含固定产出工具的代码示例、复制编辑和通用调用的接入位置，以及对应测试要求。

扩展必须保持：输入和成功的中间成果保留、编辑先复制、产出暂存后发布、工作区写互斥、后台任务与链路记录完整。不能通过增加白名单或任意代码执行绕过安全适配。现有数据策略与限制见 [安全、任务与历史说明](docs/safety-history.md)。

## 测试

`data/` 是生成的样例数据，不随 Git 提交；首次运行需要样例的测试前，先运行 `scripts/make-sample-data.bat`，并按本机安装位置调整其中的 ArcGIS Python 路径。`.arcgis-mcp/` 中的运行状态、审计数据库和恢复记录也不随仓库发布。

```bat
.venv\Scripts\python.exe tests\test_resolver.py          :: 30 项 路径解析（不需要 arcpy）
.venv\Scripts\python.exe tests\test_project.py           :: 36 项 项目根目录：识别/扫描/优先级
.venv\Scripts\python.exe tests\gate_check.py             :: 33 项 「必须先 use_project」这个约束真的生效
.venv\Scripts\python.exe tests\relative_path_check.py    :: 19 项 端到端：use_project + ./ 相对路径
.venv\Scripts\python.exe tests\project_scope_check.py    :: 29 项 端到端：多项目切换、并发会话
.venv\Scripts\python.exe tests\smoke_client.py           :: 13 项 端到端：各工具基本功能
.venv\Scripts\python.exe tests	ools_check.py            :: 162 项 端到端：**65 个工具全部覆盖**，逐个真跑
.venv\Scripts\python.exe tests\geoprocessing_check.py   :: 41 项 端到端：分析工具真跑真落盘
.venv\Scripts\python.exe tests\workflow_check.py        :: 41 项 端到端：跑通「栅格转面→消除15亩→平滑→
                                                       筛选寒冻土→相交→转单部件→消除→赋值→更新→融合」整条流程
.venv\Scripts\python.exe tests\stdio_check.py            :: 验证 stdio 模式
scripts\make-sample-data.bat                             :: 生成示例数据 data\Demo.gdb + data\shp
```

> 端到端测试需要先启动服务；改了服务端代码要重启服务才会生效。

## 常见问题

**第一次调用很慢？**
arcpy 的 `import` 要十几到几十秒。服务启动时会后台预热，正常情况下第一次调用已经是热的。
若客户端仍报超时，把客户端的工具超时（Codex 的 `tool_timeout_sec`）调到 300 秒，
或调大配置里的 `startup_timeout`。

**报 “找不到地理数据库 / 要素类”？**
错误信息里会给出当时用的项目根目录、搜索过的目录、可用名称和相近候选，直接给 AI 看即可。

**端口被占用 / 启动报 `error while attempting to bind`？**
已经有一个实例在跑（可能是上次没关掉的）。换端口 `--port 8766`，或先结束旧进程。

**`pip install` 报代理错误？**
本机 pip 走了不可用的代理。`scripts\install.bat` 已改用清华镜像；手动装时加
`--index-url https://pypi.tuna.tsinghua.edu.cn/simple`。

**gdb 被 ArcGIS Pro 占用？**
arcpy 读文件地理数据库通常允许并发读；若 Pro 正在编辑并持有锁，可能报锁错误，关掉编辑会话再试。

**`scripts\*.bat` 报 “'xxx' 不是内部或外部命令” 或输出乱码？**
批处理必须是 **纯 ASCII + CRLF 行尾**。cmd.exe 按系统 OEM 代码页（中文系统是 GBK）解析 .bat，
UTF-8 的中文注释会被误读，甚至把换行符吞掉、让注释和下一行粘成一条命令执行。改脚本时只写英文注释。
Python 文件（.py）不受影响。

**想读栅格数据？**
当前 `read_gdb_data` 只支持矢量要素类和表；栅格会在列举时出现，但读取会明确提示不支持。

## 来源

Codex 的 MCP 配置写法参考 OpenAI Codex 官方配置文档与社区实践：
[Codex config.md](https://github.com/openai/codex/blob/main/docs/config.md)、
[GitHub MCP server 相关讨论](https://github.com/github/mcp-server/discussions)。
