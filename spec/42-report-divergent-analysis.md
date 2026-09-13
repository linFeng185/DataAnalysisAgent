# 报表智能发散分析设计（扩单元 · 深挖 · 关联 · 口径）

> 状态：设计待评审（2026-09-12 按四项决策修订重写）
> 关联：spec/30-report-workbench.md（报表工作台总设计）、spec/32-metric-catalog.md（指标目录）、spec/41-report-visual-templates.md（视觉模板，正交）
> 关联功能点：features/23-report-workbench.md（23.16 确定性深挖分析、23.17 发散单元生成与档位额度、23.18 跨源分布级关联与影响判定、23.19 指标口径溯源、23.20 预览裁剪与计划单调删减）
> **已同步修订**：`spec/30-report-workbench.md:264`（见第 18.1 节）

---

## 1. 背景与问题

### 1.1 诉求

当前报表"用户说了什么就只生成什么"。用户希望系统有发散思维：

1. 除用户明确要求的数据外，主动获取**相关数据**做联合分析；
2. 判断相关数据**是否对目标数据有影响**、相关性如何；
3. 报表页面要"设计得高级一点"（已由 `spec/41` 单独处理，与本设计正交）。

### 1.2 现状：报表路径一条发散逻辑都不接

| 能力 | 现状 | 位置 |
|---|---|---|
| 主动扩单元（计划期） | **无**。`_infer_dimensions`（`planner.py:306`）与 `_infer_metrics`（`planner.py:291`）只把用户已写出的关键词贴成标签，不新增分析 | `src/reporting/planner.py` |
| 主动扩单元（运行期） | **无**。报表子图是单向无环的（`report_workbench.py:316-325`） | `src/graph/subgraphs/report_workbench.py` |
| 统计加工 | **能力已实现但报表路径不接**：`src/tools/processors.py:1` 提供同比/环比/趋势/汇总/分布/排名/占比/异常/留存/漏斗/RFM/帕累托/相关性/增长率/季节/贡献度 | `src/tools/processors.py` |
| 跨单元关联 | 部分存在：`relations.py` 已算 Pearson `correlation`、匹配率、基数，但只发布证据块，**不输出方向与强度结论** | `src/reporting/relations.py` |
| 跨源关联 | **不分析**，直接返回 `unsupported_cross_source` | `relations.py:309-316` |
| 指标口径引用 | **能力已实现但报表路径不接**：`resolve_metric_reference()`（`metric_catalog.py:250-276`）能把显式指标引用解析为已批准版本的口径文本与来源 | `src/knowledge/metric_catalog.py` |
| 图表类型 | **无推断**，报表图表固定 `bar` | `document.py:256` |

### 1.3 必须一并处理的既有缺陷

本设计触及这些代码，缺陷必须一起修，否则会被发散放大。

| # | 缺陷 | 位置 | 影响 |
|---|---|---|---|
| 1 | **`report_analyze_units` 节点在代码中不存在**（`spec/30:217` 声称存在） | 节点清单 `report_workbench.py:316-325` | 规格与代码不符 |
| 2 | **`report_profile_results` 只做 `len(rows)` 计数**（`spec/30:215` 声称算数值列/时间粒度/缺失/异常/代表性） | `report_workbench.py:225-236` | 规格与代码不符 |
| 3 | **报表路径未注入 `capability="report"`**，`classify_intent`（`contracts.py:275-282`）把单元问句判为 `sql_analysis`，旧的自主补查回环（`workflow.py:237-281`）**实际可达**（`spec/30:264` 声称不可达） | `runner.py:207-219` | 发散会让单元数上升，放大了这个未受控回环的代价 |
| 4 | **`yoy` 与 `mom` 的 `process()` 实现完全相同**（都是 `v[i] vs v[i-1]`），但 `yoy` 输出被标为"同比均值" —— 它算的是**环比** | `processors.py:13` 与 `:26` | 复用 processors 会把错误标签带进 23.16 的"同比"数字 |
| 5 | **`UnitResult` 不携带截断信号**，`rows` 上限 1000（`models.py:267`） | — | 无法判断样本是否被截断，统计结论的样本披露没有依据。**截断信号在旧工作流 state 里其实已经存在**（`query_result_statistics.truncated`、`query_result_truncated`，见 `execute_sql.py:182-183`），只是 `runner._default_execute_unit` 构造 `UnitResult` 时丢弃了它 |
| 6 | **图表硬编码 `bar`** | `document.py:256` | 时间序列与分布结果用柱状图表达不合理 |

### 1.4 关键约束（不可突破）

| 约束 | 来源 | 对本设计的影响 |
|---|---|---|
| **不得宣称因果** | `report_synthesize_insights` 明文"不把统计关系升级为因果结论"（`report_workbench.py:252`）；`features/23.4` 亦声明"只发布可验证关系证据，不宣称因果关系" | 影响判定止于相关性 + 证据分级 |
| **SQL 不支持跨源 join** | 既有能力边界 | 跨源只能做分布级对比，不能做行级连接 |
| **客户端不能自行签发执行范围** | `report_workbench.py:243-253`（`plan_override` 必须哈希完全相同） | 裁剪只能走"单调删减"，不允许客户端新增 |
| **成本必须可预占** | 报表配额机制（`reporting/quota.py`，`runner.py:72` 预占 / `:157` 释放） | 运行期发散必须按**最坏情况**预占，不允许"先跑再按实际补扣" |
| **样本有界** | `UnitResult.rows` 上限 1000（`models.py:267`）；下发页面的数据清单 ≤100 行（`document.py:230`） | 统计结论必须携带样本量披露 |
| **LLM 调用统一走工厂** | 项目 CLAUDE.md | 新增 prompt 必须注册进 `PROMPT_REGISTRY` 并经 `invoke_structured` |

---

## 2. 目标与非目标

### 2.1 目标

1. **两阶段发散**：取数前按表结构发散（计划期），取数后按实际数据发散（运行期），后者是用户明确要求的"基于已有数据判断还该查什么"。
2. **成本绝对可预占**：档位是**一次性总授权额度**（不是每轮授权），运行期发散不得突破额度，且预留必须在第一轮前完成。
3. 确定性加工**复用既有 `src/tools/processors.py`**，不重复实现。
4. 跨源单元之间能给出**分布级**关联结论，并明确披露"未做行级连接"。
5. 影响判定输出方向 + 强度 + 证据分级 + 局限，**不越界到因果**。
6. 指标口径溯源复用既有 `resolve_metric_reference()`，产出 `source_citation` block。
7. 用户在计划预览页可逐项裁剪计划期发散内容。

### 2.2 非目标

见第 17 节。

---

## 3. 总体架构与数据流

### 3.1 两个发散阶段

```text
阶段一 · 计划期发散（schema 驱动）——取数前，只看得到表名清单
  POST /plans { prompt, datasources, divergence }
    ├─ planner.parse()      [report.plan]      ← 忠实拆解用户需求 → origin="user"
    ├─ planner.diverge()    [report.diverge]   ← 仅当额度 > 0 → origin="diverged"
    └─ 返回 ReportPlan（两类单元全部进 canonical_hash）

用户在预览页裁剪 → 提交 accepted_unit_ids
    ↓
POST /jobs { plan_hash, accepted_unit_ids, template_id, ... }
    ├─ 校验 accepted ⊆ 预览计划 unit_ids
    ├─ 构造 effective plan
    └─ 落库：plan_json = effective，plan_hash = effective 的哈希，
             preview_hash = 预览凭证
```

```text
阶段二 · 运行期发散（数据驱动）——取数后，看得到实际数据
ReportRunner.run_job()
  │
  ├─[既有] report_load_policy → report_resolve_units → report_retrieve_schema
  │         → report_plan_queries → report_execute_units
  │
  ├─[新增] report_analyze_units        确定性深挖（复用 processors.py）
  │
  ├─[新增] report_decide_supplement ◄──────────── 条件边，不是节点
  │         │
  │         ├─ "supplement" → report_plan_supplement → report_execute_units
  │         │                 （round += 1，回到深挖，形成受控回路）
  │         └─ "continue"   → 继续
  │
  ├─[新增] report_attach_metric_refs   指标口径溯源（复用 metric_catalog.py）
  ├─[改写] report_build_relations      同源键匹配 + 跨源分布级关联
  ├─[改写] report_synthesize_insights  汇总（不升级为因果）
  ├─[改写] report_plan_layout          新增 findings block；图表类型用 ProcessorResult.chart_type
  └─[既有] report_validate_document
```

### 3.2 为什么两个阶段不能合并

| | 阶段一 | 阶段二 |
|---|---|---|
| 执行时机 | 计划期，**在图外**（`planner`） | 运行期，**在图内** |
| 可见信息 | 需求文本 + 单元 + 表名清单 | 上述全部 + **实际取回的数据** |
| 产出 | 新单元（需新取数） | 新单元（需新取数） |
| 用户可见性 | 预览页可逐项裁剪 | **不可裁剪**，靠档位额度约束 |
| 参数传递 | `divergence` 只传给 planner | `divergence` 写进 plan，随 job 落库，图内可读 |

**合并的代价**：若把阶段二提到计划期，就退化成"只凭表名猜"，这正是用户否定的一半；若把阶段一提掉，用户在预览页就看不到任何发散内容，违背"计划需确认"。

### 3.3 分层原则

- **确定性层**（`report_analyze_units` / `report_attach_metric_refs`）：复用既有 `processors.py` 与 `metric_catalog.py`，**无 LLM、无取数**，因此不受档位额度限制、始终开启。
- **生成式层**（`report.diverge` / `report.supplement`）：仅有的两个新增 LLM 调用，**唯一受档位额度限制**的部分。
- **证据层**（`report_build_relations`）：只产原始证据与分级，结论措辞在文档层派生。

---

## 4. 档位额度契约

### 4.1 关键设计：档位是一次性**总授权额度**

运行期发散引入了回路，如果档位表示"每轮允许几个单元"，成本会随轮数倍增。因此档位表示的是**整个 job 生命周期内允许系统自主追加的单元总数**，计划期与运行期**共享同一份额度**。

```python
DivergenceLevel = Literal["conservative", "standard", "deep"]

# 档位 → 允许系统自主追加的分析单元总数（计划期 + 运行期共享）
_DIVERGENCE_UNIT_BUDGET: dict[str, int] = {
    "conservative": 0,
    "standard": 3,
    "deep": 6,
}

# 档位 → 运行期回路最大轮数
_DIVERGENCE_MAX_ROUNDS: dict[str, int] = {
    "conservative": 0,
    "standard": 1,
    "deep": 2,
}

# 档位 → 单轮运行期追加单元上限
_DIVERGENCE_UNITS_PER_ROUND: dict[str, int] = {
    "conservative": 0,
    "standard": 2,
    "deep": 3,
}
```

| 档位 | 总追加额度 | 计划期上限 | 运行期轮数 | 单轮上限 |
|---|---|---|---|---|
| `conservative` 保守 | **0** | 0 | 0 | 0 |
| `standard` 标准（默认） | 3 | 2 | 1 | 2 |
| `deep` 深度 | 6 | 4 | 2 | 3 |

不变量（实现时必须断言）：

```text
计划期单元数 ≤ min(总额度, 12 - 用户单元数)
运行期累计追加 ≤ 总额度 - 计划期已用
总单元数 ≤ 12（既有 analysis_units 上限）
```

`conservative` 档 = 结构与现状完全一致（无任何自主追加），但确定性深挖与口径溯源照常。

### 4.2 契约字段

| 模型 | 字段 | 说明 |
|---|---|---|
| `ReportPlanRequest`（`models.py:166`） | `divergence: DivergenceLevel = "standard"` | 预览请求携带档位 |
| `ReportPlan`（`models.py:100`） | `divergence: DivergenceLevel = "standard"` | **进 `canonical_hash()`**。同一 prompt 不同档位 = 不同成本上限 = 必须进哈希 |
| `ReportPlan` | `excluded_unit_ids: list[str] = Field(default_factory=list, max_length=12)` | 相对预览删减了哪些单元（审计线索，见第 9 节），同样进哈希 |
| `AnalysisUnit`（`models.py:36`） | `origin: Literal["user", "diverged"] = "user"` | 同样进哈希 |
| `AnalysisUnit` | `rationale: str = Field(default="", max_length=500)` | **仅 `origin="diverged"` 时非空**：为什么建议分析这个 |
| `AnalysisUnit` | `decided_by: Literal["", "plan", "runtime"] = ""` | 发散单元是计划期还是运行期产生的；`origin="user"` 时必须为空 |
| `UnitResult`（`models.py:260`） | `truncated: bool = False` | 旧工作流已给出截断信号，执行器需透传而非丢弃，见 5.2 |

不变量校验加在 `AnalysisUnit.normalize_unit`（`models.py:50-59`）：

- `origin == "user"` → `rationale` 与 `decided_by` 必须为空
- `origin == "diverged"` → `rationale` 非空且 `decided_by` ∈ `{"plan", "runtime"}`

**发散单元的派生属性**（服务端强制，模型无权决定）：

| 属性 | 取值 | 理由 |
|---|---|---|
| `priority` | 固定 `"secondary"` | 发散内容不得挤占用户主需求的优先级 |
| `time_range` | **继承主单元**（`priority == "primary"` 的单元）的时间范围；主单元有多个时取**时间跨度最长**者；无主单元时用 `TimeRange` 默认值 | 同一份报表内各单元时间窗口必须一致，否则跨单元对比与同环比都无意义 |
| `dimensions` / `metrics` | 允许非空（模型可给），但仅作提示；真实列由 `report.route` 与 schema 校验决定 | 沿用既有的"模型输出只是建议"原则 |

### 4.3 用户确认的是"额度"，不是"逐项"

阶段二追加的单元**未经用户逐项确认**。这是刻意的语义转变，必须在产品上明说：

- 预览页档位选择器下方显示额度说明，例如：**"标准档：最多追加 3 个分析单元（其中至多 2 个由运行期数据决定）"**。
- 报表页面的 `limitations` **必须**包含一行：`"本次运行包含 N 个由系统基于实际数据自动追加的分析单元（档位：标准）"`。
- 保守档 = 0 个自主追加，供要求完全确定性的场景使用。

---

## 5. 确定性深挖分析

### 5.1 复用 `src/tools/processors.py`，不重复实现

`processors.py:1` 已提供本设计所需的全部能力。**不新建 `profiling.py`**。

新增 `src/reporting/profiling.py` **仅作为适配层**（约 100 行），职责是把 `ProcessorResult` 转成 `UnitFinding`，并补齐 processors 缺失的"报表级严谨性"。**统计逻辑一律不改写在报表侧。**

### 5.2 参数推导（适配层的第一职责）

`processors` 的 `process(rows, params)` 要求调用方给出 `value_col` 与 `time_col`（见 `processors.py:10`）。推导规则必须确定性：

**时间列识别**（按优先级，命中即止）：

> **实现修正（2026-09-12 落地时）**：本节原写"优先用 schema 元数据中的 `DATE`/`DATETIME`/`TIMESTAMP` 列类型"，**不可实现**。`report_retrieve_schema` 产出的 `resolved_units`（`report_workbench.py:124-129`）只带列**名**（`columns` / `dimensions` / `metrics`），**不带列类型**；要拿到类型需下钻 schema 快照。改用下面这条更强的判据。

1. **按列值能否解析为日期**：对列的前 50 个取值尝试常见日期格式（`%Y-%m-%d %H:%M:%S` / `%Y-%m-%d` / `%Y/%m/%d` / `%Y-%m` / `%Y/%m` / `%Y`），解析成功率 ≥ 80% 才认作时间列。**这比列名启发强**——它用的是真实数据，不是猜测。多列同时满足时取解析成功数最多者。
2. 无列满足阈值 → 复用既有 `_is_time_key(column)`（`relations.py:379`）做命名兜底。
3. **都没有 → 不识别为时间序列**，与时间相关的加工（环比 / 同比 / 趋势）全部跳过。**不靠列名瞎猜。**

**数值列识别**：

1. `AnalysisUnit.metrics` 与 `UnitResult.columns` 求交 → 每个命中列产一条 finding。
2. 交集为空 → 取 `rows` 中第一个**全部为数值**的列。
3. 仍无 → 跳过该单元的数值类加工。

**聚合方式**（决定同环比的分组语义）：

- 默认**求和**（覆盖销售额、订单数、访问量等绝大多数业务指标）。
- 列名或指标名匹配 `率|比|单价|均值|avg|rate|ratio|price|pct` → 改用**均值**。
- **无论走哪条，都必须写进 `method` 披露**（见 5.4）。这是启发式规则，用户需要能看到。

### 5.3 处理器映射与报表侧补强

| `UnitFinding.category` | 复用的 processor | 报表侧补强（在适配层做，不动 processors 内部） |
|---|---|---|
| `period_over_period` | `mom`（环比）与 `yoy`（同比，**需先修 bug**） | 时间跨度 < 13 个月 → **不做同比**（默认 `time_range=90d`，90 天数据算不出同比）；基期为 0 或负 → 只给绝对值不给比率 |
| `trend` | `trend` | 补 R² 计算（`numpy.polyfit`）；R² < 0.3 → 只标"趋势不显著"，不给方向性措辞 |
| `anomaly` | `anomaly` | 补 **IQR 1.5 倍 与 MAD 修正 z-score（3.5）双判据**（长尾分布下单靠 IQR 会误报）；异常占比 > 20% → 整体放弃 |
| `contribution` | `pareto` / `contribution` / `rank` | 维度基数 > 50 → 不产出；累计占比截止 80% |
| `distribution` | `distribution` | **数值分布形态**：P25 / P50 / P75 + 偏度。**不产出 CR3 / HHI** —— 集中度必须有维度列才能算份额，见下方说明 |

**关于 CR3 / HHI**：集中度指标（前三占比、赫芬达尔指数）需要**维度列**才能计算份额，纯数值列算不出来。它们归入 `contribution` 类别（该类别本就有维度列），`distribution` 只做数值分布形态。

**关于 `yoy` 的既有 bug（1.3 第 4 条）**：`YoYProcessor.process`（`processors.py:13`）与 `MoMProcessor.process`（`:26`）逐字相同，都是 `(v[i]-v[i-1])/|v[i-1]|`。`yoy` 被标为"同比均值"，实际算的是环比。

修复方案（**必须做**，否则 23.16 的同比数字是错的）：

- `mom` 保持现状（环比 = 与上一期比）。
- `yoy` 改为与**同周期去年**比：月度粒度用 `v[i] vs v[i-12]`，季度粒度用 `v[i] vs v[i-4]`，周粒度用 `v[i] vs v[i-52]`。
- 期数不足以回看一个完整周期 → 返回 `ProcessorResult("数据不足", [], "table", rows, "low")`（沿用该文件既有的"数据不足"出口）。
- 这是对**聊天链路共享文件**的修改，必须跑既有回归（`tests/test_tools/`）确认无破坏。

### 5.4 输出契约与样本披露

```python
class FindingItem(BaseModel):
    label: str = Field(..., max_length=200)            # 如 "华东 · 销售额"
    value: float | None = None
    unit: str = Field(default="", max_length=32)
    change_ratio: float | None = None
    baseline: str = Field(default="", max_length=64)   # 对比基准描述
    significance: Literal["high", "moderate", "low", "unknown"] = "unknown"
    caveats: list[str] = Field(default_factory=list, max_length=10)

class UnitFinding(BaseModel):
    unit_id: str
    category: Literal["period_over_period", "trend", "anomaly", "contribution", "distribution"]
    title: str = Field(..., max_length=200)
    items: list[FindingItem] = Field(default_factory=list, max_length=50)
    method: str = Field(..., max_length=500)           # 必填：数字怎么算出来的
    chart_type: str = Field(default="table", max_length=16)  # 直接取自 ProcessorResult
    limitations: list[str] = Field(default_factory=list, max_length=10)
    truncated: bool = False
```

**样本披露**：

- `truncated = UnitResult.truncated`，直接取自旧工作流 state 的 `query_result_statistics.truncated`（`execute_sql.py:182`）。
- **不新增 `total_rows` 字段**：原始行数在现有链路里不可得（`rows` 已被连接器上限截过），而截断信号本就存在，只是 `runner` 构造 `UnitResult` 时丢弃了它。为拿不到的精确分母去下钻 `sql_execution.py` 与 `execute_sql.py` 得不偿失，且这两个文件正被另一条并行开发线修改。
- 若 `truncated=True`，**每个 finding 强制携带** `caveats: ["样本被截断：结论仅基于返回的 N 行计算"]`。沿用 `RelationEvidence.sampled`（`relations.py:35`）的既有做法。
- 截断信号缺失时按 `False` 处理，`method` 里仍写明"基于 N 行"。

**每个 finding 的 `method` 必填**，且必须包含：用了哪些列、按什么分组、用了什么聚合、用了什么判据。这是零幻觉与可解释性的底线。

---

## 6. 两个发散阶段

### 6.1 阶段一：计划期发散 `report.diverge`

**为什么独立于 `report.plan`**：该 prompt 的职责是"忠实拆解用户需求"（`prompts.py:357-368` 明文含"完整保留用户原意"）。把它改成"顺便发散"会让它不再忠实 —— 模型会为凑发散内容而曲解需求。

注册进 `PROMPT_REGISTRY`（`src/llm/prompts.py`），与 `report.plan`（`:353`）、`report.route`（`:372`）并列：

```python
"report.diverge": PromptDefinition(
    prompt_id="report.diverge",
    version="1.0.0",
    task="report_diverge",
    template=(
        "你是报表分析发散助手。给定用户已确认的分析单元、每个数据源的表名清单和本次允许的建议数量上限，"
        "提出最多 {limit} 个'用户没要求但会影响其结论'的补充分析单元。"
        "每个建议必须说明 rationale（为什么与已有需求相关）与 expected_signal（预期能看到什么信号）。"
        "datasource 只能从给定清单里选，不确定时留空字符串，禁止凭猜测填写。"
        "禁止提出与已有单元语义重复的建议；禁止在 rationale 里使用'导致''造成''因为'等因果措辞，"
        "只能描述相关性与共变关系。"
        "只输出 JSON：{{\"proposed_units\": [{{\"question\": \"...\", \"datasource\": \"\", "
        "\"rationale\": \"...\", \"expected_signal\": \"...\"}}]}}。"
        "确实没有值得补充的方向时返回空数组，不要为凑数编造。"
    ),
    output_model=ReportDivergeOutput,
    # 与 report.route（prompts.py:385）对齐：发散同样需要看全表名清单才能建议"还可以查什么"。
    context_budget=40000,
),
```

### 6.2 阶段二：运行期发散 `report.supplement`

**这是用户明确要求的"基于已有数据判断还该查什么"。**

新增 prompt，输入是**实际数据加工后的发现**，不是表名猜测：

```python
"report.supplement": PromptDefinition(
    prompt_id="report.supplement",
    version="1.0.0",
    task="report_supplement",
    template=(
        "你是报表分析补充助手。给定已有分析单元、这些单元实际取回数据后计算出的显著发现"
        "（同环比、趋势、异常、贡献度、分布），以及每个数据源的表名清单，"
        "判断是否存在'为了解释或验证这些发现而需要补充查询'的分析单元。"
        "只有当发现确实指向一个尚未覆盖的方向时才提出建议，每条必须说明 supports_finding"
        "（它解释或验证哪一条已有发现）。"
        "datasource 只能从给定清单里选，不确定时留空字符串。"
        "禁止使用'导致''造成''因为'等因果措辞，只能描述相关性与共变关系。"
        "只输出 JSON：{{\"proposed_units\": [{{\"question\": \"...\", \"datasource\": \"\", "
        "\"rationale\": \"...\", \"supports_finding\": \"...\"}}]}}。"
        "没有需要补充的方向时返回空数组——这是最常见也最正确的结果，不要为凑数编造。"
    ),
    output_model=ReportSupplementOutput,
    context_budget=40000,
),
```

### 6.3 回路决策：确定性前置筛选（省 LLM 调用）

`report_decide_supplement` 是**条件边函数而非节点**（与 `after_analyze_result`，`workflow.py:237-281` 同构）。它先用确定性规则筛选，**四条全满足才调用 LLM**：

```text
1. 已追加单元数 < 档位总额度
2. 本轮次 < _DIVERGENCE_MAX_ROUNDS[level]
3. 当前单元数 < 12（全局上限）
4. 本轮深挖产出至少一条 significance ∈ {high, moderate} 的 finding
   —— 若深挖什么都没发现，说明数据正常，无需补查
```

任一不满足 → 直接返回 `"continue"`，**不消耗 LLM 调用**。

满足时调用 `report.supplement`：

- 返回空数组 → `"continue"`（正常路径，**不写 `warnings`**）
- 返回非空 → 截断至 `min(_DIVERGENCE_UNITS_PER_ROUND[level], 剩余额度)` → 生成 `origin="diverged"`、`decided_by="runtime"` 的单元 → 返回 `"supplement"`

### 6.4 两阶段的模型输出处理（统一原则）

阶段一与阶段二共用同一套服务端处理（沿用 `_coerce_llm_units`，`planner.py:420-449` 的既有原则）：

1. **丢弃模型给出的任何 `unit_id`**（输出契约里根本没有该字段），由服务端 `uuid4` 重新分配。
2. `datasource` 不在已授权清单内 → **清空**（沿用"模型在授权集合内的猜测会被清空"的既有模式），由 `report.route` 重新定位。
3. 超出额度 → 截断 + `warnings`。
4. `rationale` 命中因果措辞黑名单（`导致|造成|因为|引起|致使|归因于`）→ 丢弃该条 + `warnings`。提示词约束是软约束，**服务端必须硬校验**。
5. `priority` 固定 `"secondary"`；阶段二额外要求 `supports_finding` 非空，否则丢弃（防止无依据的补查）。

### 6.5 降级路径

| 场景 | 行为 |
|---|---|
| 档位为 `conservative` | 两个阶段都不调用，不写 `warnings` |
| `is_task_llm_available(<task>)` 为 False | 跳过，写 `warnings: ["发散分析不可用：未配置可用模型，仅按原需求生成"]` |
| LLM 调用异常 | 捕获，写 `warnings`，**主链路继续** |
| 模型返回空数组 | 正常路径，不写 `warnings` |
| 运行期回路达到轮数上限 | 正常终止，不写 `warnings` |
| 运行期追加单元执行失败 | 该单元 `status="failed"`，其余照常；已有结论不受影响 |

---

## 7. 跨源分布级关联

### 7.1 现状缺口

`analyze_same_source`（`relations.py:279-373`）在跨源且仅有结构信息时返回 `unsupported_cross_source`（`:309-316`）。用户选择了"允许跨源扩单元"，若跨源单元之间无法分析关联，扩源就只能多拿几个孤立结论。

### 7.2 新增 `analyze_cross_source`

```python
# 方法作用：在不做行级连接的前提下，对跨数据源的两个单元做分布级秩相关分析。
# Args: left - 左侧单元数据集；right - 右侧单元数据集。
# Returns: 标注为 cross_source_statistical 的关系证据，无法支撑时返回 insufficient。
def analyze_cross_source(
    self,
    left: dict[str, Any] | RelationDataset,
    right: dict[str, Any] | RelationDataset,
) -> RelationEvidence
```

**算法（严格受限）**：

1. 两侧 `status` 必须为 `success`，否则返回 `blocked`（沿用 `analyze_same_source` 的处理）。
2. 找**共同指标列**（不是 join key —— 跨源不做键匹配）。无共同指标 → `insufficient`，说明"无共同可比指标"。
3. **时间对齐**：两侧都有可识别时间列（复用 5.2 的识别规则）且粒度可归一 → 按时间做**内连接**对齐。对齐后观测点 < 8 → `insufficient`，说明"两侧时间点重合不足"。
4. 无时间列 → 对两侧各自的数值序列做秩相关，并在 `limitations` 注明"未按时间对齐，仅分布形态相关"。
5. 对数值序列做 **Spearman 秩相关**（`pandas.DataFrame.corr(method="spearman")`）。
6. 结果写进既有 `RelationEvidence`：
   - `relation_type = "cross_source_distribution"`
   - `correlation` = Spearman 系数
   - `evidence_level = "cross_source_statistical"`
   - `join_keys = []`、`match_rate = None`（**不适用，不得伪造**）
   - `limitations` **强制包含**：`["跨数据源未做行级连接，结论仅基于各自样本分布的秩相关，不能推断个体级关联，不构成因果"]`
7. 任一侧数值列方差为 0 → `insufficient`，说明"数值列无有效变化"。

**不做的**：不估算连接行数、不猜测业务粒度、不做行级 join、不把字段同名当作关系。与 `aggregation.py:26-36` 的同类禁令保持一致。

### 7.3 调度位置

`build_relations()`（`relations.py:387-421`）已对所有单元做 `combinations(datasets, 2)` 全组合。改动：在组合循环内按 `left.datasource == right.datasource` 分派到 `analyze_same_source` 或 `analyze_cross_source`，**接口签名不变**，调用方 `report_build_relations` 无需改动。

---

## 8. 指标口径溯源（23.19 由"基准对照"改为此）

### 8.1 为什么改

原设计"从指标目录取目标值做差"**建立在错误假设上**：`MetricDefinition`（`metric_catalog.py:28-38`）的字段只有 `code / name / datasource / dialect / expression / description / unit` —— **没有目标值或基准值字段**。指标目录提供的是**业务口径公式**，不是目标值。

### 8.2 改为口径溯源

> **实现修正（2026-09-12 落地时）**：本节原写"调用 `resolve_metric_reference()` 按 `AnalysisUnit.metrics` 名称与别名精确匹配"，**这是错的**。真实的既有机制是 `resolve_metric_references()`（复数，`metric_catalog.py:253`），它解析的是**问题文本里的 `@metric(code)` 显式引用**，不存在按名称匹配的路径。且该解析**已经由既有工作流完成**：`retrieve_schema.py:147-157` 在单元执行时调用它，结果写入 state 的 `activated_knowledge`。因此报表侧无需任何新查询与 LLM 调用，只要把结果筛出来呈现。下文按修正后的机制描述。

**数据来源**：旧工作流 state 的 `activated_knowledge` 列表。其中 `source_id` 以 `metric:` 开头的条目即指标口径来源（形如 `metric:{tenant}:{code}:v{version}`），`locator` 内含 `metric_code` / `version` / `name` / `expression` / `unit`。

**落地路径（三段，各司其职）**：

| 环节 | 位置 | 职责 |
|---|---|---|
| 提取 | `runner._extract_metric_citations()` | 按 `metric:` 前缀筛选、按 `metric_code` 去重、丢弃结构不完整的条目 |
| 汇总 | 新节点 `report_attach_metric_refs` | 汇总各单元的 `UnitResult.metric_citations` 并全局去重；保守档返回空（见 8.3） |
| 成块 | `_append_metric_citations()`（由 `report_plan_layout` 调用） | 追加 `source_citation` 区段，携带可读 `summary` 与结构化 `items` |

- `source_citation` 已在 `_BLOCK_TYPES`（`document.py:27`）与 `layout_for_block`（`document.py:198`）中定义但**从未有产出路径**，本设计正好填上这个落差。
- **不解析 `description` 里的非结构化文字**，不做任何数值推断。
- 页面块的脚本特征校验（`document.py:105-107`）只检查 `content` 的**顶层字符串值**，因此嵌套在 `items` 里的口径表达式不会被拦；进入顶层 `summary` 的文本会被拦。区段构建失败时**降级为不展示并记 warning**，不让装饰性来源块导致整份报表失败。

`content` 结构：

```json
{
  "citation_kind": "metric_catalog",
  "items": [{
    "code": "gmv",
    "name": "成交总额",
    "expression": "SUM(order_amount)",
    "version": 3,
    "effective_at": "2026-07-01"
  }],
  "summary": "本报表的成交总额按租户指标目录 v3 已批准口径计算"
}
```

### 8.3 降级

| 场景 | 行为 |
|---|---|
| 档位为 `conservative` | 不执行（保持该档"完全不加料"的语义） |
| 单元无 `metrics` | 跳过该单元 |
| 指标名未命中目录 | 不产出该条，**不猜测** |
| 目录无已批准版本 | 跳过，不写 `warnings`（正常路径） |
| 目录查询异常 | 写 `warnings`，主链路继续 |

---

## 9. 裁剪机制：`accepted_unit_ids` 单调删减

### 9.1 现状约束

`plan_override` **必须是哈希完全相同的拷贝**（`report_workbench.py:243-253`），即现有设计**不允许客户端修改计划**。这是刻意的防篡改设计，不能简单放开。

### 9.2 方案

新增 `ReportJobCreateRequest.accepted_unit_ids: list[str] | None = Field(default=None, max_length=12)`。

**`None` 与 `[]` 语义必须区分**，否则"不传"与"全部取消"无法表达：

| 取值 | 语义 |
|---|---|
| `None`（不传，默认） | 全部接受，行为与现状**完全一致** |
| 非空列表 | 只保留列表中的单元 |
| `[]`（显式空数组） | 422 —— 空计划无意义 |

服务端 `create_job` 的处理顺序：

```text
1. 既有缓存查找与 plan_override 校验（完全不变）
2. 若 accepted_unit_ids is None → effective = 预览计划（行为与现在完全一致）
3. 若非 None：
   a. 校验非空，否则 422（[] 是显式的"全部取消"，属非法请求）
   b. 校验 accepted ⊆ {u.unit_id for u in 预览计划.analysis_units}，否则 422
   c. 校验与 plan_override 未同时提交，否则 422（语义冲突）
   d. effective = 预览计划保留已接受单元后 model_validate 重建
      （单元被删后 excluded_unit_ids 记录被删的 id；重新触发 unit_id 唯一性、
        requested_datasources 重算等全部既有校验）
4. 执行与落库用 effective
```

**为什么这不构成越权**：`accepted_unit_ids` 只能**删减**缓存中的预览计划，不能新增。执行范围只可能更小，与"客户端不能自行签发执行范围"不冲突。校验步骤 3b 是关键 —— 没有它，客户端就能凭空构造范围。

**裁剪只作用于计划期单元**。运行期追加的单元由档位额度约束，不能被裁剪（用户看不到它们产生）。

### 9.3 哈希语义

| 列 | 存什么 | 语义 |
|---|---|---|
| `plan_json` | **effective plan**（含 `excluded_unit_ids`） | 实际执行范围 |
| `plan_hash` | effective plan 的 `canonical_hash()` | **语义不变**：仍是 `plan_json` 的校验和 |
| `preview_hash`（新列） | 客户端提交的预览凭证哈希 | 审计溯源："这份计划来自哪次预览" |

裁剪为空时 `plan_hash == preview_hash`。**新增 `preview_hash` 列是为了不改变 `plan_hash` 的既有语义** —— 它现在确实是 `plan_json` 的校验和（`store.py:128`）。

`plan_hash` 目前**只有两个消费者**：落库（`store.py:128`）与预览凭证校验（`report_workbench.py:245,258`）。`grep plan_hash src/reporting/runner.py` 零命中，runner 不读它，因此拆分不影响执行链路。

### 9.4 新迁移 `migrations/028_report_job_preview_hash.sql`

```sql
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS preview_hash VARCHAR(80);
```

应用层约束：非空时必须是 `sha256:` + 64 位十六进制。

---

## 10. 页面呈现

### 10.1 新增 block 类型 `findings`

`_BLOCK_TYPES`（`document.py:27`）与 `ReportBlock.type` 白名单新增 `findings`。前端 `ReportBlockType`（`types/report.ts:126`）同步。

`content` 结构：

```json
{
  "category": "anomaly",
  "method": "按 month 分组求和；IQR 1.5 倍与 MAD 修正 z-score(3.5) 双判据",
  "truncated": true,
  "items": [{ "label": "华东 · 销售额", "value": 0, "unit": "元", "change_ratio": -0.42,
              "baseline": "上月", "significance": "high",
              "caveats": ["样本被截断：本次仅基于 1000/5327 行计算"] }]
}
```

前端渲染为结构化列表：`method` 作为可展开说明（让用户知道数字怎么来的），`caveats` 逐条显示。

### 10.2 `relationship` block 扩展

`RelationEvidence`（`relations.py:19-35`）**不加冗余字段**：`correlation` 是带符号的，方向与强度在 `build_report_document()` 生成 content 时派生。

| content 字段 | 派生规则 |
|---|---|
| `direction` | `correlation > 0.05` → `positive`；`< - 0.05` → `negative`；否则 `unknown` |
| `strength` | `abs(correlation) < 0.3` → `weak`；`0.3 ~ 0.6` → `moderate`；`> 0.6` → `strong`；null → `unknown` |
| `cross_source` | `evidence_level == "cross_source_statistical"` |

### 10.3 图表类型（修 1.3 第 6 条）

**直接使用 `ProcessorResult.chart_type`**（`processors.py:16` 等已给出 `"line"` / `"table"` 等），不再自行映射。该字段已在 `processors.py` 内部按处理器语义确定：

- `yoy` / `mom` → `line`
- `trend` → 取处理器输出
- 其余 → 取处理器输出

回退规则：`chart_type` 为空或不在 `_SUPPORTED_CHART_TYPES`（`chart_generator.py:17`）内 → `bar`（保持现状）。**不调用 LLM 判定。**

### 10.4 预览页分组、额度说明与裁剪 UI

`ReportWorkbenchPage.tsx` 计划预览区域改为：

| 分组 | 来源 | 交互 |
|---|---|---|
| 你要求的分析 | `origin == "user"` | 只读展示 |
| 建议补充的分析 | `origin == "diverged"` 且 `decided_by == "plan"` | 逐项 `Checkbox`（默认全选），展示 `rationale` 与 `expected_signal` |

档位 `Segmented`（保守 / 标准 / 深度）放在**点"预览"之前**（与数据源选择器并列）。切换档位本身不触发任何调用；下次点"预览"时生效。

档位下方常驻额度说明文案（见 4.3），并在报表页 `limitations` 里披露运行期追加的单元数。

---

## 11. 配额与成本

### 11.1 必须一起修的旧回环漏洞（1.3 第 3 条）

`runner.py:207-219` 的 `app.ainvoke` 未注入 `capability="report"`，`classify_intent`（`contracts.py:275-282`）会把单元问句判为 `sql_analysis`，从而允许触发 `after_analyze_result` 的**旧**自主补查回环（`workflow.py:237-281`）。该回环轮数上限默认 2（`max_supplemental_query_rounds`）、**无配额预占、无档位控制**。

**修复**：`_default_execute_unit` 的 state 显式注入 `capability="report"`（该字段已是 state 的既有键），使报表路径按 `spec/30:264` 的声明**真的不进入旧回环**。

**必须最先做**。理由：本设计新增了报表**自己的**受控回路，若旧的未受控回路仍然可达，两套回路会叠加，成本失控。

### 11.2 成本归属

| 环节 | 新增查询 | 新增 LLM 调用 |
|---|---|---|
| `report.diverge`（计划期） | 0 | 1（整批一次，仅档位 > 保守） |
| `report.supplement`（运行期） | 0 | 最多 `_DIVERGENCE_MAX_ROUNDS[level]` 次 |
| 追加单元执行 | 追加单元数 × 命中的数据源数 | 同左 × 完整工作流节点数 |
| 确定性深挖（processors） | 0 | 0 |
| 跨源分布级关联 | 0 | 0 |
| 指标口径溯源 | 0 | 0 |

### 11.3 预占必须覆盖最坏情况

`runner._reserve_plan`（`runner.py:305-324`）现在按**真实扇出**修正一次。本设计引入回路后，必须在第一轮执行前按**最坏情况**预占：

```text
预留额外查询 = 档位总额度 × 假定的单单元数据源数
```

理由：回路是数据驱动的，无法在预占时知道会不会发生。**不允许"先跑再按实际补扣"** —— 那会绕过配额。

`runner._reserve_plan` 已按真实扇出修正，因此保守预占不会超扣；实际用量更少则在 `finally`（`runner.py:157-161`）释放。

### 11.4 `query_count` 上限必须提高

`EstimatedResources.query_count` 当前是 `Field(default=1, ge=0, le=16)`（`models.py:95`），而用户单元 12 + 档位额度 6 = 18 个单元，每个单元还可能命中多个数据源（`ReportUnitRoute.datasources` 上限 20，`output_contracts.py:171`），**现有上限必然被击穿**。

设计决定：提高到 `le=64`（18 个单元 × 平均命中 4 个源的保守估计）。这不影响实际扣费 —— 预占与扣费都按真实扇出修正，估算值只决定预占的保守程度。

---

## 12. 错误处理与降级

| 场景 | 行为 |
|---|---|
| 档位为 `conservative` | 两个阶段的发散都不调用、不做口径溯源；深挖与跨源关联照常 |
| 发散 LLM 不可用 / 异常 | 写 `warnings`，主链路继续；报表仍正常生成 |
| 发散返回空数组 | 正常路径，不写 `warnings` |
| 发散建议超额度 | 截断 + `warnings` 说明 |
| 建议的 `rationale` 含因果措辞 | 丢弃该条 + `warnings` |
| 建议的 `datasource` 越权 | 清空该字段，由 `report.route` 重新定位 |
| 运行期决策的四条前置条件任一不满足 | 直接终止回路，**不调用 LLM**，不写 `warnings` |
| 运行期达到轮数上限 | 正常终止，不写 `warnings` |
| 运行期追加单元执行失败 | 该单元 `status="failed"`，其余照常 |
| 深挖：时间列无法识别 | 跳过所有时间相关处理器，**不猜列名** |
| 深挖：数值列无法识别 | 跳过数值类加工，不产出 |
| 深挖：观测点不足 | 该类 finding 不产出，**不降级到猜测** |
| 深挖：`processors` 返回"数据不足" | 该条不产出（沿用处理器既有出口） |
| 截断信号缺失（`query_result_statistics` 无 `truncated`） | 按未截断处理，`method` 里仍写明"基于 N 行" |
| 行集被截断 | `truncated=True`，每个 finding 携带样本披露 |
| 跨源：无共同指标 / 时间重合 < 8 / 方差为 0 | `evidence_level = "insufficient"` + 说明 |
| 指标目录查询异常 | 写 `warnings`，主链路继续 |
| 指标名未命中目录 | 不产出，**不猜测** |
| `accepted_unit_ids` 不传（`None`） | 全部接受，行为与现状完全一致 |
| `accepted_unit_ids` 为显式空数组 `[]` | 422 |
| `accepted_unit_ids` 含非预览单元 | 422 |
| `accepted_unit_ids` 与 `plan_override` 同时提交 | 422 |
| `preview_hash` 格式非法 | 422 |

原则与 `spec/41` 一致：**用户可见处静默降级并留痕，开发者可见处（内部 API 参数）明确报错**。

---

## 13. 目录结构

```text
src/reporting/
├── models.py                                    [修改] DivergenceLevel、AnalysisUnit.origin/rationale/decided_by、
│                                                        ReportPlan.divergence/excluded_unit_ids、
│                                                        ReportPlanRequest.divergence、
│                                                        ReportJobCreateRequest.accepted_unit_ids、
│                                                        UnitResult.truncated、
│                                                        EstimatedResources.query_count 上限 16→64
├── planner.py                                   [修改] diverge() 与 _coerce_diverge_units()；按档位额度估算
├── profiling.py                                 [新增] 适配层：参数推导 + ProcessorResult → UnitFinding
│                                                        （统计逻辑全部复用 processors，不重写）
├── relations.py                                 [修改] 新增 analyze_cross_source；build_relations 按源分派
├── document.py                                  [修改] _BLOCK_TYPES 加 findings；relationship 派生
│                                                        direction/strength；source_citation 产出；图表类型
└── store.py                                     [修改] 落库 preview_hash 与 effective plan

src/tools/
└── processors.py                                [修改] 修 yoy 的既有 bug（当前算的是环比）；
                                                        按需补 R² / 双判据参数
                                                        ⚠ 聊天链路共享文件，须跑 tests/test_tools/ 回归

src/llm/
├── prompts.py                                   [修改] 注册 report.diverge 与 report.supplement
└── output_contracts.py                          [修改] ReportDivergeOutput / ReportSupplementOutput

src/graph/subgraphs/
└── report_workbench.py                          [修改] 新增 report_analyze_units、report_attach_metric_refs、
                                                        report_plan_supplement；
                                                        新增条件边 report_decide_supplement；
                                                        修正节点清单（对齐 spec/30:215-217）

src/reporting/runner.py                          [修改] 1) _default_execute_unit 注入 capability="report"
                                                        （前置修复，见 11.1）
                                                        2) _reserve_plan 按最坏情况预占（见 11.3）

src/api/routes/report_workbench.py               [修改] /plans 透传 divergence；/jobs 处理 accepted_unit_ids

migrations/
└── 028_report_job_preview_hash.sql              [新增] ALTER TABLE report_jobs ADD COLUMN preview_hash

frontend/src/
├── types/report.ts                              [修改] ReportBlockType 加 findings；relationship content 字段
├── components/report/ReportDocumentRenderer.tsx [修改] findings 渲染分支；relationship 显示方向/强度；
│                                                        source_citation 渲染口径来源
└── pages/ReportWorkbenchPage.tsx                [修改] 档位选择器 + 额度说明文案 + 建议单元分组勾选
```

---

## 14. 安全与边界

| 边界 | 处理 |
|---|---|
| **运行期扩大执行范围** | 用户确认的是**档位额度**而非逐项。额度进 `canonical_hash`，运行期追加不得突破额度与 12 单元全局上限；报表页 `limitations` 强制披露运行期追加数量 |
| 客户端签发执行范围 | `accepted_unit_ids` 只允许**删减**预览计划的子集；执行范围不可能变大 |
| 计划防篡改 | `plan_override` 的哈希校验逻辑**完全不变**；裁剪走独立参数，二者互斥 |
| 成本失控 | 档位总额度 + 轮数上限 + 单轮上限 + 12 单元上限 + **最坏情况预占** 五重约束 |
| 旧回环叠加 | 11.1 注入 `capability="report"` 关闭旧回环，避免与新的受控回路叠加 |
| 因果越界 | 提示词软约束 + 服务端因果措辞黑名单硬校验；`report_synthesize_insights` 既有"不升级为因果"原则不变 |
| 越权数据源 | 发散建议的 `datasource` 不在授权清单时清空；仍走既有 `report.route` + `_resolve_plan_access`，无旁路 |
| 脱敏 | 深挖与跨源关联都在**已脱敏**的行集上进行（沿用既有链路），不新增原始数据出口 |
| 行/列/样本上界 | 深挖与关联都受 `RelationPolicy`（`relations.py:38-45`）与 `UnitResult` 1000 行上限约束 |
| SQL 注入 | 深挖为纯内存计算，不生成 SQL；跨源关联不做 join，不生成 SQL |
| 提示词注入 | 深挖的 finding 文本进入 `report.supplement` 输入前必须截断（`title` / `items` 均有 `max_length`）；表名与列名来自 schema 而非用户输入 |

---

## 15. 测试

### 15.1 `tests/test_reporting/test_divergence.py`（新增）

| 测试函数 | 场景 |
|---|---|
| `test_divergence_budgets_match_design` | 表格断言总额度 0/3/6、轮数 0/1/2、单轮 0/2/3 |
| `test_conservative_skips_both_diverge_calls` | 保守档下 `report.diverge` 与 `report.supplement` **零调用** |
| `test_plan_divergence_capped_by_budget_and_global_limit` | 用户已有 11 个单元 → 计划期只能加 1 个 |
| `test_runtime_divergence_shares_budget_with_plan_phase` | 计划期用了 2 个（标准档）→ 运行期只剩 1 个 |
| `test_runtime_loop_stops_at_max_rounds` | 深度档 → 最多 2 轮，第 3 轮不进入 |
| `test_runtime_decision_skips_llm_when_no_significant_finding` | 无 high/moderate finding → **不调用 LLM** 直接终止 |
| `test_runtime_supplement_requires_supports_finding` | 缺 `supports_finding` 的建议被丢弃 |
| `test_diverge_failure_keeps_main_plan` | 发散抛异常 → 计划仍生成，`warnings` 含降级说明 |
| `test_diverge_empty_result_is_not_a_warning` | 返回空数组 → 无 `warnings` |
| `test_diverge_units_get_server_assigned_ids` | 服务端重分配 `unit_id` |
| `test_diverge_clears_unauthorized_datasource` | 越权 `datasource` 被清空 |
| `test_diverge_rejects_causal_rationale` | `rationale` 含"导致" → 丢弃 + `warnings` |
| `test_decided_by_invariant` | `origin="user"` 时 `decided_by` 必须为空；`origin="diverged"` 时必须为 `plan`/`runtime` |
| `test_diverged_units_inherit_primary_time_range` | 主单元 `time_range=365d` → 发散单元继承 `365d`；多主单元取最长跨度 |
| `test_diverged_units_priority_is_secondary` | 模型给出 `priority="primary"` 的建议 → 被强制为 `secondary` |
| `test_origin_divergence_and_excluded_ids_enter_canonical_hash` | 三者任一变化 → `canonical_hash()` 变化 |
| `test_reserve_plan_covers_worst_case_rounds` | 预占覆盖 (1 + 轮数上限) 的扇出（**防绕过配额核心用例**） |
| `test_limitations_disclose_runtime_appended_units` | 存在 `decided_by="runtime"` 单元时，页面 `limitations` 含披露文案 |

### 15.2 `tests/test_tools/test_processors.py`（扩展，回归）

| 测试函数 | 场景 |
|---|---|
| `test_yoy_compares_same_period_last_year` | 构造 24 期月度数据，断言 `yoy` 用 `v[i] vs v[i-12]` 而非 `v[i] vs v[i-1]`（**修 bug 的核心用例**） |
| `test_yoy_returns_insufficient_without_full_cycle` | 期数 < 13（月度）→ 返回"数据不足" |
| `test_mom_still_compares_previous_period` | `mom` 行为不变（防止修 `yoy` 时误改 `mom`） |
| `test_existing_processor_regressions` | 既有聊天链路用例全部保持通过 |

### 15.3 `tests/test_reporting/test_profiling_adapter.py`（新增）

适配层用例。**统计数值本身由 `tests/test_tools/` 覆盖，这里只测适配与披露。**

| 测试函数 | 场景 |
|---|---|
| `test_time_column_prefers_schema_types` | schema 有 DATE 列 → 用它，忽略列名启发 |
| `test_time_column_falls_back_to_is_time_key` | 无 schema 类型 → 用 `relations._is_time_key` |
| `test_time_column_absent_skips_temporal_processors` | 都无法识别 → `yoy`/`mom`/`trend` 全部不调用 |
| `test_value_column_prefers_metrics_intersection` | 指标与列交集优先 |
| `test_value_column_falls_back_to_first_numeric` | 无交集 → 第一个全数值列 |
| `test_aggregation_uses_mean_for_rate_like_names` | 列名含"率" → 均值；否则求和 |
| `test_aggregation_choice_is_disclosed_in_method` | 聚合方式必须出现在 `method` 文本里 |
| `test_yoy_skipped_when_span_below_thirteen_months` | 90 天数据 → 不做同比 |
| `test_zero_baseline_omits_change_ratio` | 基期为 0/负 → 只给绝对值 |
| `test_r_squared_below_threshold_marks_insignificant` | R² < 0.3 → 不给方向性措辞 |
| `test_anomaly_requires_both_iqr_and_mad` | 长尾数据单命中 IQR → 不判异常 |
| `test_anomaly_abandons_when_outlier_ratio_too_high` | 异常占比 > 20% → 整体不产出 |
| `test_contribution_skips_high_cardinality` | 维度基数 > 50 → 不产出 |
| `test_distribution_reports_percentiles_not_concentration` | `distribution` 产出 P25/P50/P75 + 偏度，**不产出 CR3/HHI** |
| `test_internal_error_is_adapted_not_propagated` | `processors` 抛异常 → 该条不产出，不中断整份报表 |
| `test_truncated_input_marks_every_finding` | `truncated=True` → 每个 finding 带样本披露 |
| `test_missing_truncation_signal_is_treated_as_not_truncated` | 信号缺失 → 不判截断，`method` 写行数 |
| `test_findings_carry_method_description` | `method` 必填且非空 |
| `test_chart_type_comes_from_processor_result` | `chart_type` 直接取自 `ProcessorResult`，不自行映射 |
| `test_unknown_chart_type_falls_back_to_bar` | 非法 `chart_type` → `bar` |

### 15.4 `tests/test_reporting/test_cross_source_relations.py`（新增）

| 测试函数 | 场景 |
|---|---|
| `test_cross_source_aligns_by_time_and_returns_spearman` | 两侧有时间列 → 内连接对齐后秩相关 |
| `test_cross_source_insufficient_when_time_overlap_too_small` | 对齐后观测点 < 8 → `insufficient` |
| `test_cross_source_without_time_uses_distribution` | 无时间列 → 分布级相关 + 对应 `limitations` |
| `test_cross_source_without_common_metric_is_insufficient` | 无共同指标 → `insufficient` |
| `test_cross_source_limitations_always_forbid_causality` | `limitations` 必含"不构成因果"（**合规断言**） |
| `test_cross_source_never_fakes_join_keys_or_match_rate` | `join_keys == []` 且 `match_rate is None` |
| `test_cross_source_constant_column_is_insufficient` | 方差为 0 → `insufficient` |
| `test_build_relations_dispatches_by_datasource` | 同源走键匹配、跨源走分布级，签名不变 |

### 15.5 `tests/test_reporting/test_metric_reference.py`（新增）

| 测试函数 | 场景 |
|---|---|
| `test_metric_reference_matches_exact_name` | 名精确命中 → 产出 `source_citation` content |
| `test_metric_reference_returns_nothing_for_unknown_metric` | 未命中 → 不产出、不猜测 |
| `test_metric_reference_failure_is_warning_only` | 目录异常 → `warnings`，主链路继续 |
| `test_conservative_skips_metric_reference` | 保守档 → 不执行 |
| `test_source_citation_block_is_actually_produced` | `source_citation` 从"定义了无产出"变为有产出（落差回归断言） |

### 15.6 `tests/test_reporting/test_plan_trimming.py`（新增）

| 测试函数 | 场景 |
|---|---|
| `test_missing_accepted_ids_keeps_full_plan` | 不传（`None`）→ 行为与现状完全一致 |
| `test_explicit_empty_accepted_ids_is_rejected` | 显式 `[]` → 422 |
| `test_accepted_ids_subset_keeps_only_listed_units` | 只保留勾选单元 |
| `test_unknown_unit_id_is_rejected` | 非预览单元 id → 422（**防越权核心用例**） |
| `test_accepted_ids_with_plan_override_is_rejected` | 同时提交 → 422 |
| `test_trimmed_ids_recorded_in_excluded_unit_ids` | 被删的 id 记入 effective plan 的 `excluded_unit_ids` |
| `test_effective_plan_revalidates_datasources` | 裁剪后 `requested_datasources` 正确重算 |
| `test_plan_hash_stores_effective_checksum` | `plan_json` 与 `plan_hash` 自洽 |
| `test_preview_hash_records_original_credential` | `preview_hash` 等于客户端提交值 |
| `test_preview_hash_equals_plan_hash_when_no_trimming` | 未裁剪时两者相等 |
| `test_runner_injects_report_capability` | `_default_execute_unit` 注入 `capability="report"`（11.1 前置修复回归） |

### 15.7 前端契约 `tests/test_frontend/test_report_findings.py`（新增）

沿用既有 Python 静态断言模式。

| 测试函数 | 断言 |
|---|---|
| `test_renderer_has_findings_branch` | `switch` 含 `findings` 分支 |
| `test_renderer_shows_method_and_caveats` | 渲染 `method` 与 `caveats` |
| `test_relationship_shows_direction_and_strength` | 关系块渲染 `direction` 与 `strength` |
| `test_workbench_has_divergence_selector_and_budget_hint` | 工作台含档位选择器与**额度说明文案** |
| `test_workbench_sends_divergence_and_accepted_ids` | `api/reportWorkbench.ts` 传递 `divergence` 与 `accepted_unit_ids` |
| `test_types_include_findings_block_type` | `ReportBlockType` 含 `findings` |

### 15.8 浏览器验收（`RUN_BROWSER_TESTS=1`）

沿用 `test_report_browser.py` 的 `page.route("**/api/v1/**")` 假数据拦截模式（该文件 `:90-125` 为范例），不访问业务后端。

`tests/test_frontend/test_divergence_browser.py`：

- 预览页切换档位 → 额度说明文案随之变化；点击预览后建议单元分组数量符合档位。
- 取消勾选若干建议单元 → 创建任务 → 断言请求体里的 `accepted_unit_ids` 正确。
- 报表页面渲染 `findings` block，`method` 可展开。

---

## 16. 实施顺序

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **A（前置修复）** | 1) `runner` 注入 `capability="report"`；2) 修 `processors.yoy` 的既有 bug；3) `UnitResult.truncated` 透传；4) `query_count` 上限 | 无，**必须最先做** |
| B | 契约与档位额度（`models.py` + planner 估算 + `test_divergence.py` 契约部分） | 无，与 A 并行 |
| C | 深挖适配层（`profiling.py` + `report_analyze_units` 节点 + `test_profiling_adapter.py`） | A、B |
| D | 计划期发散（`prompts.py` `report.diverge` + `planner.diverge`） | B |
| E | 运行期回路（`report.supplement` + `report_decide_supplement` 条件边 + `report_plan_supplement`） | C、D |
| F | 跨源分布级关联（`relations.py`） | C |
| G | 指标口径溯源（`report_attach_metric_refs` + `source_citation` 产出） | C |
| H | 裁剪机制（迁移 `028` + API + store） | B |
| I | 页面呈现（`document.py` findings + 前端渲染 + 预览页 UI + 契约测试） | C/E/F/G |
| J | 浏览器端到端验收 | I |

C / D / F / G / H 之间无相互依赖，可并行；E 依赖 C 与 D。

---

## 17. 明确不做（YAGNI）

| 不做 | 原因 |
|---|---|
| 因果推断 / 反事实分析 / 工具变量 | 数据是脱敏样本、无实验设计，任何因果结论都是过度声称。与项目既有"不宣称因果"原则冲突 |
| 跨源行级 join | SQL 层不支持；应用层内存 join 会把两侧数据拉到一起，突破脱敏与行数上界的现有边界 |
| 运行期发散超过 2 轮 | 每轮都是完整工作流；超过 2 轮的成本与收益不成比例。且回路轮数无法在预占时确定，轮数越少预占越准 |
| 运行期追加单元的用户逐项确认 | 那会退化成同步阻塞的交互式分析，与报表的批处理/定时属性冲突。改为额度授权 + 披露 |
| 时序预测 / 预测区间 | 样本有界且可能被截断，预测误差无法诚实披露；属独立能力 |
| 聚类 / 分群 / 用户画像 | 需全量数据与稳定主键，与脱敏样本约束冲突 |
| 深度模型的异常检测（Isolation Forest 等） | 样本量支撑不起，且黑盒结果无法向用户解释 `method`。IQR + MAD 可解释性是刻意选择 |
| 从 `MetricDefinition.description` 解析目标值 | 那是自由文本，解析必然猜测。等指标目录显式增加目标值字段再做 |
| 新建 `profiling.py` 重写统计逻辑 | `processors.py` 已实现，重复实现必然漂移 |
| 让 LLM 决定档位或自动加码 | 成本决策必须由用户显式做出 |
| 修改 `canonical_hash()` 的排除项 | 模板已走独立载体（`spec/41`）；计划哈希语义保持不动 |
| 前端 `insight_list` 的产出路径 | `findings` 承担结构化洞察，`source_citation` 由口径溯源填充；`insight_list` 保持未使用，留给后续 |

---

## 18. 与既有规格的关系

### 18.1 `spec/30-report-workbench.md:264` 已同步修订

原文声称"报表路径**默认不进入**旧的单轮自主补查回环"。本设计仍保证这一点（11.1 注入 `capability="report"` 关闭**旧的** `after_analyze_result` 回环），但新增了**报表自己的**受控回路，因此该节已补充限定语：

> 报表路径**不进入旧的单轮自主补查回环**（由 `runner.py` 注入 `capability="report"` 关闭）……**报表自身的发散补充回路**是独立机制，与上面被关闭的旧回环无关：它在已取回数据之后运行，受档位额度（保守 0 / 标准 3 / 深度 6，计划期与运行期共享）、轮数上限（0/1/2）与单轮上限（0/2/3）三重约束；进入回路前必须先通过确定性前置筛选，无显著发现时不消耗 LLM 调用；`runner` 必须按最坏情况预占配额；页面 `limitations` 必须披露自主追加单元数量。

修订已按项目规则（CLAUDE.md「发现 SPEC 设计有问题：先提出修改方案，用户确认后修改 `spec/`，再修改代码」）随本设计一并落盘。

### 18.2 落差处理对照

| 落差 | 本设计的处理 |
|---|---|
| `spec/30:217` `report_analyze_units` 节点不存在 | **补齐**：用该名称作为深挖节点，节点清单同步更新 |
| `spec/30:215` `report_profile_results` 只数行数 | **补齐**：由 `report_analyze_units` 承担真实深挖；`report_profile_results` 保留其行数统计职责或并入前者，实现时二选一并更新 `spec/30` |
| `spec/30:264` 报表路径不进补查回环 | **修正代码 + 修订规格**：11.1 注入 `capability="report"`；补充限定语见 18.1 |
| `processors.yoy` 实为环比 | **修 bug**：阶段 A |
| `UnitResult` 丢弃了旧工作流已有的截断信号 | **透传 `truncated`**：阶段 A。不新增 `total_rows`——原始行数在现有链路不可得 |
| 图表硬编码 `bar` | **修正**：用 `ProcessorResult.chart_type`，回退仍为 `bar` |
| `source_citation` 定义了无产出 | **补齐**：口径溯源产出该 block |
| `document.navigation` / `document.sources` 未消费 | 仍不在本文范围，见 `spec/41` 第 16 节 |
