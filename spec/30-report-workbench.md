# 报表工作台与自然语言自动化设计

> 状态：Phase 1 基础闭环及 Phase 2 本轮编号能力已实现并进入回归验收；真实 PostgreSQL 多 Worker/RLS 和外部模型仍需独立验收。接口与运行约束见 `34-report-phase2-implementation.md`。
> 适用版本：Phase 3 生产化增强，首批实现建议拆分为 P1；依赖现有数据源 ACL、SQL 安全执行、LangGraph、分析引擎、可视化引擎和租户管理。

## 1. 背景与问题

现有自动化能力（`analysis_schedules`、`ScheduledAnalysisRunner`、`AutomationPage`）解决的是“保存一条固定只读 SQL，按频率执行并发送 Markdown 摘要”。它不能完成以下工作：

1. 用户不能只输入一段自然语言来生成报表任务；创建表单要求用户预先填写 SQL、数据源和频率。
2. `report_subgraph` 只消费当前轮一次查询的样本，调用固定 `weekly_report/monthly_report` 模板，输出 Markdown，不是可浏览、可分享的报表页面。
3. 报表没有任务级运行次数、生命周期、过期清理、永久保留和分享授权模型。
4. 页面没有稳定的交互数据契约，图表点击不能安全地查看明细；将任意 SQL 或脚本交给浏览器会产生越权和 XSS 风险。
5. 多业务数据只按当前请求的单一 SQL 处理，没有“先拆分业务问题、再判断可联合关系、最后分别分析并综合”的编排。
6. 长流程的节点级重试、失败恢复、运行日志和用户可见错误不完整，无法判断任务是没有执行、权限失败、SQL 失败还是渲染失败。
7. 租户管理员不能按用户配置菜单权限、周期内报表次数和 Token 预算。

本规格将上述能力定义为独立的 `Report Workbench`（报表工作台），不破坏现有主动洞察和旧定时报告接口。旧能力作为兼容适配器保留，新的自然语言报表任务使用新的任务、运行和页面模型。

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 验收要点 |
|---|---|---|
| G1 | 自然语言创建任务 | 用户输入文本后得到结构化执行计划；没有明确调度词时默认立即执行一次。 |
| G2 | 生成富报表页面 | 页面由受控 JSON DSL 描述，包含摘要、指标、图表、表格、洞察、关系分析和来源，不使用固定模板或任意 HTML。 |
| G3 | 发散且有证据的分析 | 每个业务数据集先独立分析，再基于可验证的键、时间或指标关系做联合分析；不能把相关性伪装成因果性。 |
| G4 | 任务和页面生命周期 | 页面默认七天有效，可延期；具备授权的用户可以设置永久有效；到期后页面、交互快照和分享入口不可访问并异步清理。 |
| G5 | 安全交互与分享 | 图表点击只调用服务端生成的 drilldown 定义；公开分享只返回脱敏快照，不暴露 SQL、凭证、权限过滤规则或实时数据库。 |
| G6 | 租户治理 | 管理员控制菜单/能力权限、每个用户的周期报表次数、并发数、查询行数和 Token 预算。 |
| G7 | 可恢复执行 | 节点级重试、运行级重试、取消、超时、失败原因、日志和 trace_id 完整可查。 |

### 2.2 非目标

- 首期不允许报表任务写入数据库、执行 DDL/DML、调用外部动作或自动修改业务系统。
- 首期不生成任意 React/HTML/JavaScript；页面只能由后端签发的白名单 DSL 渲染。
- 首期不把公开分享链接当作实时数据源代理；公开链接默认展示固定脱敏快照。
- 首期不承诺从多个业务系统推导因果关系。只能展示直接证据、统计关系或明确标注的假设。
- 首期不删除或改变旧 `analysis_schedules` 的语义；迁移采用并行运行和兼容查询。

## 3. 术语与角色

| 术语 | 定义 |
|---|---|
| 报表任务（ReportJob） | 用户输入、执行计划、生命周期和配额关联的长期对象。 |
| 报表运行（ReportRun） | 报表任务的一次实际执行，包含节点步骤、重试和产物版本。 |
| 报表版本（ReportVersion） | 某次成功运行生成的不可变页面快照和数据清单。 |
| 分析单元（AnalysisUnit） | 一个可独立取数和分析的业务问题，例如“订单金额趋势”或“客户留存分层”。 |
| 关系证据（RelationEvidence） | 两个分析单元之间可以联合的键、时间窗口、匹配率、相关系数或来源引用。 |
| 页面 DSL（ReportDocument） | 后端签发、前端按白名单渲染的布局和内容 JSON。 |
| 钻取（Drilldown） | 用户点击图表或指标后查看同一快照中更细粒度的明细，或在授权用户场景下重新执行受限查询。 |
| 公开分享 | 未登录用户通过高熵 token 访问脱敏快照；可选额外授权码。 |
| 周期 | 租户配置的配额统计周期，默认自然月，时区默认为 `Asia/Shanghai`。 |

角色能力采用“菜单能力”和“数据访问”分离原则：

| 能力 | `super_admin` | `tenant_admin` | `analyst` | `viewer` |
|---|---:|---:|---:|---:|
| 查看报表工作台 | 全部 | 按租户策略 | 按用户授权 | 按用户授权 |
| 创建/运行报表 | 全部 | 按租户策略 | `report.create` | 默认否 |
| 延期/永久保留 | 全部 | 可配置 | `report.extend`（默认否） | 否 |
| 创建/撤销分享 | 全部 | 可配置 | `report.share`（默认否） | 否 |
| 配置用户权限和配额 | 平台策略 | 当前租户 | 否 | 否 |
| 查看他人报表 | 平台策略 | 当前租户 | 仅显式共享 | 仅显式共享 |

即使角色拥有菜单，仍必须通过当前用户的数据源、列级权限、行过滤、Skill 授权和租户 RLS。菜单权限不能扩大数据权限。

## 4. 总体架构

```text
用户自然语言
    │
    ▼
ReportWorkbench API
    ├─ 身份、菜单能力、周期配额、幂等校验
    ├─ ReportPlanner（规则抽取 + 结构化 LLM 解析）
    └─ 计划预览 / 确认
            │
            ▼
       ReportJob
            │  immediate: 后台启动一次
            │  scheduled: Scheduler 原子认领
            ▼
ReportWorkbenchSubgraph（每次运行独立 thread_id）
    ├─ load_current_policy
    ├─ resolve_business_units
    ├─ retrieve_authorized_schema
    ├─ plan_queries
    ├─ execute_units（复用 SQL 子图，有限并发）
    ├─ build_relation_evidence
    ├─ analyze_units（确定性指标 + 结构化 LLM）
    ├─ synthesize_cross_business_insights
    ├─ plan_layout
    ├─ validate_document
    └─ persist_version
            │
            ▼
     ReportVersion / ReportDocument
            ├─ 私有工作台查看
            ├─ 授权分享快照
            └─ 安全 Drilldown API
```

### 4.1 与现有链路的关系

1. `classify_intent` 增加 `capability=report_workbench`，保留原 `capability=report` 给旧报告路径。
2. 新能力使用独立 `report_workbench_subgraph`，不把大量分析步骤塞进当前单轮 `AnalysisState` 的 `query_result_sample`。
3. 每个 `AnalysisUnit` 的 SQL 必须复用现有 `retrieve_schema → generate_sql → layer3_validate → layer4_explain → execute_sql` 子图装配函数，禁止为报表另写一套绕过 ACL 的执行器。
4. 报表运行使用独立 `report_run_id` 和 LangGraph `thread_id`。页面内容、数据清单和错误摘要落专用存储，不将完整大结果写入普通聊天 checkpoint。
5. 旧 `analysis_schedules` 可由适配器展示在“兼容任务”分组；旧任务仍由 `ScheduledAnalysisRunner` 执行，升级后的新任务不强行写入旧表。

## 5. 自然语言任务计划

### 5.1 计划输入

```json
{
  "prompt": "生成销售、库存和客户留存报表，先立即跑一次；以后每周一上午九点执行，共执行 4 次，页面保留 30 天并允许团队成员查看",
  "datasources": ["sales", "warehouse"],
  "model_id": "",
  "llm_connection_id": null,
  "timezone": "Asia/Shanghai",
  "confirm": false,
  "idempotency_key": "client-generated-key"
}
```

请求体不允许提交 `tenant_id`、`user_id`、权限快照、原始 SQL、Webhook URL 或渠道凭证。数据源为空时，Planner 只能在当前身份已授权的数据源候选中选择；完全无法确定时返回需要用户选择的计划，不自动探测无权限来源。

### 5.2 解析规则与默认值

解析分为确定性规则抽取和结构化 LLM 归一化两步。规则结果优先约束 LLM，LLM 不得覆盖安全、权限、配额或用户明确的时间/次数。

| 文本特征 | 结构化结果 |
|---|---|
| “立即、现在、立刻、执行一次、先跑一次” | `execution_mode=immediate`，默认 `max_runs=1`；“先跑一次以后……”可同时产生一次性运行和后续计划。 |
| “每天/每周/每月/每小时”及明确时间 | `execution_mode=scheduled`，生成 `frequency`、`run_at`、`timezone`。 |
| “执行 N 次、连续 N 天、跑到某日期” | 设置 `max_runs` 或 `schedule_end_at`，两者同时出现时取更早终止条件。 |
| 只有分析内容，没有调度词 | 默认立即执行一次，不创建周期调度。 |
| 只有“定时/以后”但没有周期或时间 | 返回 `needs_confirmation=true`，不擅自创建周期任务。 |
| “永久保存/永不过期” | 仅在当前身份拥有 `report.permanent` 时生效，否则返回权限错误并建议七天默认期限。 |
| 相互冲突的次数、日期、频率 | 返回冲突字段和可读提示，必须确认后才执行。 |

默认值：`execution_mode=immediate`、`max_runs=1`、页面 `expires_in=7d`、`share.allow_drilldown=false`、`timezone=租户时区`、`max_concurrency=4`。定时任务若未指定次数，默认持续到被暂停或到 `schedule_end_at`，但仍受租户总配额和最长调度期限约束。

### 5.3 计划契约

```json
{
  "plan_version": "1.0",
  "execution_mode": "immediate",
  "frequency": null,
  "run_at": null,
  "timezone": "Asia/Shanghai",
  "max_runs": 1,
  "schedule_end_at": null,
  "page_ttl_days": 7,
  "permanent": false,
  "requested_datasources": ["sales", "warehouse"],
  "analysis_units": [
    {
      "unit_id": "sales_trend",
      "question": "销售额、订单数和客单价的近期趋势",
      "priority": "primary",
      "time_range": {"kind": "relative", "value": "90d"},
      "dimensions": ["日期", "产品"],
      "metrics": ["销售额", "订单数", "客单价"]
    },
    {
      "unit_id": "retention",
      "question": "客户留存与复购表现",
      "priority": "primary",
      "time_range": {"kind": "relative", "value": "90d"},
      "dimensions": ["客户分层"],
      "metrics": ["留存率", "复购率"]
    }
  ],
  "share": {
    "enabled": false,
    "require_code": false,
    "allow_drilldown": false,
    "scope": "private"
  },
  "warnings": [],
  "needs_confirmation": false,
  "estimated": {
    "query_count": 2,
    "llm_tokens": 6500,
    "estimated_seconds": 20
  }
}
```

计划必须通过 Pydantic `ReportPlan` 校验：数组长度、时间范围、频率、次数、页面 TTL、数据源数量、Token 预算和 `unit_id` 字符集均有上限。计划中不保存 LLM 原始思维链。

### 5.4 计划确认策略

- `POST /report-workbench/plans` 只解析和估算，不产生任务、不扣除运行配额。
- 计划无歧义、用户明确说“立即生成/现在执行”时，`POST /report-workbench/jobs` 可直接创建并启动。
- 计划包含周期、永久、公开分享、预计超出用户默认配额或存在冲突时，必须先返回预览，前端要求用户点击确认；服务端再次校验计划 hash，防止前端篡改。
- 同一个 `idempotency_key + identity + plan_hash` 在有效窗口内只产生一个 Job；重复提交返回已有 Job。

## 6. 报表 LangGraph 子图

### 6.1 节点与输出

| 节点 | 作用 | 失败是否可重试 | 关键输出 |
|---|---|---:|---|
| `report_load_policy` | 读取当前身份、用户能力、数据源权限、行列规则和配额快照 | 否 | `PermissionContext`、`QuotaSnapshot` |
| `report_resolve_units` | 把计划中的业务问题绑定到授权数据源、表和指标 | 是（LLM 解析） | `ResolvedAnalysisUnit[]` |
| `report_retrieve_schema` | 按单元按需加载相关 Schema、可信业务规则和指标口径 | 是 | `SchemaEvidence[]` |
| `report_plan_queries` | 为每个单元生成一个或多个只读查询计划，禁止直接执行 SQL | 是 | `QueryPlan[]` |
| `report_execute_units` | 有界并行执行查询，复用 SQL 安全和 ACL 链路 | 瞬态错误可重试 | `UnitResult[]` |
| `report_profile_results` | 统计每个单元返回行数，无数据时补充可读说明 | 否（确定性） | `UnitResult[]`（带 warnings） |
| `report_analyze_units` | 对已取回数据做确定性深挖：环比、同比（跨度≥一年）、趋势、异常、贡献度、分布形态。统计逻辑复用 `src/tools/processors.py`，报表侧只做参数推导与严谨性补强（IQR+MAD 双判据、R² 门槛、样本截断披露），**不经过 LLM** | 否（确定性） | `UnitFinding[]` |
| `report_build_relations` | 发现并验证跨单元的键、时间窗口和指标关系 | 是（确定性优先） | `RelationEvidence[]` |
| `report_synthesize_insights` | 汇总单元并做交叉分析，区分事实、统计关系和假设 | 是（结构化输出修复） | `CrossBusinessAnalysis` |
| `report_attach_metric_refs` | 汇总单元问题里 `@metric(...)` 引用解析出的已批准口径，供页面做口径溯源 | 否（确定性） | `MetricCitation[]` |
| `report_plan_layout` | 根据内容重要性、数据类型和屏幕断点规划页面块 | 是 | `ReportDocument` |
| `report_validate_document` | 校验 DSL、引用、数量、脱敏标记、交互指令和资源大小 | 否，失败需修复/降级 | `ValidatedReportDocument` |
| `report_persist_version` | 以不可变版本写页面、数据清单和摘要，更新 Job 状态 | 事务可重试 | `ReportVersion` |

所有节点只传递显式 Pydantic 契约。完整查询结果、原始 Prompt、凭证、权限规则和推理内容不得进入公开页面；大结果进入受控对象存储或压缩表，状态中只保留引用、摘要和有界样本。

### 6.2 多业务数据与发散分析

每个 `AnalysisUnit` 先独立完成以下确定性分析（可按数据类型跳过不适用项）：

- 指标总量、均值、中位数、分位数、环比/同比、移动平均和贡献度；
- 时间趋势、季节性提示、分布、缺失率、重复率和异常点；
- 分类 Top N、集中度、分层差异、长尾和结构变化；
- 数据质量、时间覆盖、截断和权限限制；
- 基于可见证据的业务解释候选和行动建议。

跨业务联合分析必须经过 `RelationEvidence`：

```json
{
  "left_unit_id": "sales_trend",
  "right_unit_id": "retention",
  "relation_type": "entity_time_join",
  "join_keys": ["customer_id", "week"],
  "match_rate": 0.87,
  "left_rows": 12000,
  "right_rows": 8300,
  "cardinality": "many_to_one",
  "correlation": 0.62,
  "time_alignment": "same_week",
  "evidence_level": "verified_relation",
  "limitations": ["未控制渠道和价格因素"]
}
```

规则：

1. 只在双方列权限均允许、键类型兼容、匹配率达到租户配置阈值（默认 0.6）、基数不会产生不可控膨胀时构建联合结果。
2. 跨数据源先各自执行授权 SQL，再在受控分析器中按列投影、采样或聚合后联合；不得把一侧的连接凭证交给另一侧 Connector。
3. 找不到可靠键时可以做时间窗口、共同维度或统计相关分析，但页面必须标注 `statistical_association`，不得写成“导致/因果”。
4. 完全不相关的数据仍分别生成完整分析单元，并在总览中说明“未发现可验证关联”，不能为了页面美观硬拼成一张表。
5. LLM 只能选择已存在的 `RelationEvidence` 和指标结果，不能自行创造字段、数值、连接键或因果结论。

### 6.3 补查策略

报表路径**不进入旧的单轮自主补查回环**（`src/graph/workflow.py:237-281`，由 `src/reporting/runner.py` 在单元执行时注入 `capability="report"` 关闭）。缺失证据由 `report_plan_queries` 一次性生成有界查询计划：

- 每个单元最多 `primary_query_limit=2` 条主查询和 `evidence_query_limit=2` 条证据查询；
- 同一表、同一粒度、同一筛选条件的缺口合并为一条查询；
- 总查询数默认不超过 16，超过时按单元优先级截断并在页面显示限制；
- 只读、安全、行列权限、限流、脱敏和审计在每条查询上重新执行；
- 查询失败不覆盖其他单元，页面显示该单元状态和可重试操作。

**报表自身的发散补充回路**（详见 `spec/42-report-divergent-analysis.md` 第 4、6 节）是独立机制，与上面被关闭的旧回环无关：

- 它在**已取回数据**之后运行，基于确定性深挖产出的显著发现决定是否补充查询，因此属于"数据驱动"而非"证据不足驱动"；
- 它受**档位额度**（保守 0 / 标准 3 / 深度 6 个自主追加单元，计划期与运行期共享同一份额度）、**轮数上限**（0/1/2）与**单轮上限**（0/2/3）三重约束；
- 进入回路前必须先通过确定性前置筛选，无显著发现时**不消耗 LLM 调用**；
- 运行期追加单元无法在预占时确定，因此 `runner` 必须按**最坏情况**预占配额，不允许"先跑再按实际补扣"；
- 页面 `limitations` 必须披露本次运行包含的自主追加单元数量。

## 7. 报表页面 DSL

### 7.1 设计原则

页面内容由后端产生 `ReportDocument`，前端只渲染注册组件。LLM 可以提出布局建议，但不能输出可执行 HTML、CSS、JavaScript、ECharts 原始函数或 URL 跳转脚本。所有字段都经过 JSON Schema/Pydantic 校验和大小限制。

### 7.2 顶层结构

```json
{
  "schema_version": "report.document.v1",
  "report_id": "report-uuid",
  "version": 3,
  "title": "经营分析报表",
  "subtitle": "数据范围：最近 90 天",
  "generated_at": "2026-08-26T10:00:00Z",
  "expires_at": "2026-09-02T10:00:00Z",
  "theme": {"accent": "#1677ff", "density": "comfortable"},
  "navigation": ["overview", "sales", "retention", "relations", "quality"],
  "sections": [],
  "sources": [],
  "limitations": [],
  "permissions": {"download": false, "drilldown": false}
}
```

### 7.3 允许的页面块

| `block.type` | 内容 | 交互 |
|---|---|---|
| `kpi` | 数值、单位、变化、状态、证据引用 | 可打开对应分析单元 |
| `narrative` | 摘要、事实、解释、限制和建议 | 展开/收起，不允许 HTML |
| `chart` | 服务端生成的 ECharts option 子集、数据引用和说明 | 点击数据点触发已签发 Drilldown |
| `table` | 有序列定义、脱敏行、分页信息 | 查看受限明细/复制允许字段 |
| `insight_list` | 多条洞察及证据级别 | 跳转来源块 |
| `relationship` | 两个单元、关系类型、匹配率、相关性和限制 | 展开关系详情 |
| `quality` | 完整性、截断、权限和数据新鲜度 | 查看质量说明 |
| `source_citation` | 数据源、表、指标口径和生成时间摘要 | 仅显示允许的来源元数据 |
| `callout` | 警告、错误、需要用户行动 | 可触发重试或延长页面 |

每个块包含 `id`, `section_id`, `priority`, `data_ref`, `evidence_refs`, `layout`, `visibility`。块数量默认上限 80，单个文本 20,000 字符，单个表 100 行/页，页面 JSON 压缩后默认不超过 2 MB。

### 7.4 图表白名单

复用 `src/tools/chart_generator.py` 和 `features/14-visualization.md` 的图表能力：`line`、`bar`、`pie`、`scatter`、`heatmap`、`table`。服务端通过 `src/reporting/document.py::validate_echarts_option()` 只允许普通 JSON 值、白名单 axis/series/tooltip/legend/dataZoom 配置；禁止函数、事件代码、`dataView` HTML、任意 formatter 字符串和外部图片 URL。图表数据必须引用页面中的已脱敏 `data_ref`，不能自行读取数据库。

### 7.5 自适应布局

布局器先按块的内容密度、重要性、数据宽高比和依赖关系计算“布局意图”，再通过确定性算法落到 CSS Grid：

- 桌面端 12 列，平板 8 列，移动端 4 列；
- `kpi` 默认占 3/2/1 列，趋势图默认占 8/6/4 列，明细表默认整行；
- 同一关系分析的图表、摘要和限制必须相邻；
- 超长文本自动独占整行，不得遮挡后续块；
- 空数据、部分失败和图表生成失败都保留可见的 `callout`，不能产生空白卡片；
- 页面固定格式只作为最小降级方案，正常路径按内容动态排列。

前端禁止用嵌套卡片堆叠页面；区段为无框布局，卡片只用于独立重复块、明细工具和弹窗。

## 8. 交互与钻取

### 8.1 Drilldown 契约

```json
{
  "drilldown_id": "dd-uuid",
  "source_block_id": "sales-by-region",
  "mode": "snapshot",
  "allowed_filters": [
    {"field": "region", "operator": "eq", "value_type": "string"},
    {"field": "period", "operator": "between", "value_type": "date"}
  ],
  "columns": ["region", "order_count", "sales"],
  "max_rows": 100,
  "expires_at": "2026-09-02T10:00:00Z"
}
```

`mode=snapshot` 从当前报表版本的脱敏数据清单中筛选，默认用于公开分享；`mode=live` 仅对登录且仍拥有当前数据权限的租户用户开放，每次请求重新解析 ACL 和行列规则。客户端只能提交 `drilldown_id + filters + page`，不能提交 SQL、表名、列名、数据源或用户身份。

### 8.2 交互权限

分享创建者可以分别选择“查看页面”“查看脱敏明细”“允许登录用户实时钻取”“允许下载”。`scope=public` 使用高熵 token；`scope=user` 额外保存 `user_scope=specified|tenant` 与 `user_ids`，要求登录身份命中指定用户或租户成员范围。公开链接永远不能启用 `live`，也不能返回未脱敏字段。撤销分享、页面到期、用户停用、租户停用或数据权限撤销时，交互立即失效。

## 9. 生命周期与分享

### 9.1 页面生命周期

状态：`draft → planning → queued → running → succeeded | partial | failed | cancelled → expired | deleted`。

- 成功版本默认 `expires_at = generated_at + 7 days`；失败运行不生成可访问版本。
- `POST /reports/{id}/extend` 只能把过期时间向后延长，默认每次最多 30 天；租户策略配置最大延长期限。
- `POST /reports/{id}/permanent` 只能由拥有 `report.permanent` 的用户调用，设置 `expires_at=null`，但仍可被管理员删除或租户停用。
- 到期由后台清理任务按批次软删除页面版本、分享和 drilldown 数据，保留最小审计摘要 30 天；对象存储使用同一 `expires_at` 生命周期策略。
- 页面版本不可变。重新执行任务生成新版本；分享链接默认固定到创建时的版本，用户明确选择“始终查看最新版本”时才绑定 Job 的 latest 指针。

### 9.2 分享 token 与授权码

- 创建分享时服务端生成至少 128 bit 的随机 token；数据库只保存 token 的 HMAC/SHA-256 摘要，原 token 只在创建响应中返回一次。
- URL 形如 `/shared/reports/{public_token}`。token 不包含租户、用户或数据库信息，访问日志必须脱敏。
- 可选授权码使用现有密码哈希策略保存，服务端只保存哈希和失败次数；连续失败触发短时锁定和 IP/Token 限流。
- 支持 `expires_at`、`revoked_at`、访问范围、下载和 drilldown 开关。撤销后所有缓存立即失效。
- 公开访问仅返回 sanitized `ReportDocument` 和允许的 snapshot data；不返回 SQL、Schema、原始知识、权限规则、用户偏好、Prompt、Token 用量或运行日志。
- 公开响应禁止被搜索引擎索引，设置 `Cache-Control: private, no-store`；前端不把授权码或完整 token 写入持久化存储。

## 10. 权限、菜单与配额

### 10.1 能力权限

新增能力键：

| 能力键 | 默认角色 | 说明 |
|---|---|---|
| `report.menu` | `tenant_admin`、`analyst` | 是否显示报表工作台菜单 |
| `report.create` | `tenant_admin`、`analyst` | 创建和立即运行任务 |
| `report.schedule` | `tenant_admin` | 创建周期任务 |
| `report.share` | `tenant_admin` | 创建/撤销分享 |
| `report.extend` | `tenant_admin` | 延期页面 |
| `report.permanent` | `tenant_admin` | 设置永久有效 |
| `report.live_drilldown` | `tenant_admin`、被授权 `analyst` | 登录用户实时钻取 |
| `report.view_all` | `tenant_admin` | 查看本租户报表 |
| `report.download` | 按用户配置 | 下载脱敏结果 |

权限配置必须通过租户管理员页面和后端管理 API 完成。前端菜单隐藏只是体验优化，所有 API 仍在服务端鉴权。权限变更不影响历史页面的所有权判断，但会立即阻止新建、分享或实时钻取。

### 10.2 配额模型

租户管理员为每个用户配置以下周期值；未配置时使用租户默认值：

```json
{
  "period": "calendar_month",
  "timezone": "Asia/Shanghai",
  "max_jobs_created": 20,
  "max_runs_started": 50,
  "max_concurrent_runs": 2,
  "max_analysis_units_per_run": 12,
  "max_queries_per_run": 16,
  "max_rows_per_query": 100000,
  "max_llm_tokens": 200000,
  "max_page_bytes": 2097152,
  "max_share_links": 20
}
```

计费与计数规则：

1. 创建任务在事务中预占 `max_jobs_created`；取消或解析失败不计入，已进入运行的任务不能通过删除回滚配额。
2. 每次运行只计一次 `runs_started`；节点重试不重复扣运行次数，但 Token 和查询资源按实际消耗累计。
3. 多实例通过 PostgreSQL 原子 `UPDATE ... WHERE used < limit RETURNING` 预占配额，不能依赖进程内计数。
4. 超过并发、查询、行数或 Token 预算返回可读的 `QUOTA_EXCEEDED`，不执行部分 SQL；已开始的运行按失败记录并释放未使用的并发槽。
5. 租户管理员可以查看用户使用量、失败量和预估 Token，不显示 Prompt 原文或敏感查询内容。

## 11. API 契约

以下接口位于 `/api/v1/report-workbench`，所有私有接口使用现有 HttpOnly Cookie/JWT 身份；请求不得携带租户或用户覆盖值。

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/plans` | 解析自然语言、返回计划预览和资源估算，不执行 |
| `POST` | `/jobs` | 按已确认计划创建任务；立即任务异步启动，定时任务进入队列 |
| `GET` | `/jobs` | 当前用户任务；管理员可按租户筛选 |
| `GET` | `/jobs/{job_id}` | 任务详情、计划、最新运行和页面版本 |
| `POST` | `/jobs/{job_id}/run` | 立即重新运行，重新校验权限和配额 |
| `PATCH` | `/jobs/{job_id}` | 暂停、恢复、修改后续调度或取消 |
| `DELETE` | `/jobs/{job_id}` | 删除任务；已发布版本按策略保留或一并删除 |
| `GET` | `/jobs/{job_id}/runs` | 分页列出运行记录和步骤摘要 |
| `GET` | `/runs/{run_id}` | 运行状态、重试、错误和耗时；不返回敏感 Prompt |
| `GET` | `/reports/{report_id}` | 获取当前用户可访问的页面版本 |
| `GET` | `/reports/{report_id}/versions` | 列出页面版本 |
| `POST` | `/reports/{report_id}/extend` | 延长有效期 |
| `POST` | `/reports/{report_id}/permanent` | 设置永久有效 |
| `POST` | `/reports/{report_id}/shares` | 创建分享链接 |
| `GET` | `/reports/{report_id}/shares` | 列出本人可管理的分享 |
| `DELETE` | `/shares/{share_id}` | 撤销分享 |
| `POST` | `/reports/{report_id}/drilldowns/{drilldown_id}` | 查看受限快照/实时明细 |
| `GET` | `/quotas/me` | 查看本人剩余配额 |
| `GET` | `/shared/reports/{token}` | 访问公开脱敏快照 |
| `POST` | `/shared/reports/{token}/unlock` | 提交授权码并获取短时访问会话 |

### 11.1 创建 Job 示例

```json
{
  "prompt": "生成销售和库存经营报表，立即执行一次，页面保留七天",
  "plan_hash": "sha256:...",
  "plan_override": null,
  "confirm": true,
  "idempotency_key": "req-20260826-001"
}
```

```json
{
  "job_id": "job-uuid",
  "status": "queued",
  "execution_mode": "immediate",
  "run_id": "run-uuid",
  "report_id": null,
  "estimated": {"query_count": 4, "llm_tokens": 9000},
  "quota": {"runs_remaining": 49, "tokens_remaining": 191000}
}
```

### 11.2 运行状态事件

`GET /runs/{run_id}` 支持轮询；前端可以使用 SSE `/runs/{run_id}/stream`，事件只包含：`run_queued`、`node_start`、`node_progress`、`node_retry`、`node_end`、`run_partial`、`run_succeeded`、`run_failed`、`run_cancelled`。事件不包含原始 Token、SQL 凭证、完整行数据或内部 Prompt。

### 11.3 分享创建请求

```json
{
  "version": 3,
  "scope": "public",
  "require_code": true,
  "authorization_code": "user-entered-code",
  "expires_in_days": 7,
  "allow_drilldown": false,
  "allow_download": false,
  "pin_latest": false
}
```

响应只显示一次 `url` 和可选 `authorization_code_hint`；数据库和日志不保存明文授权码或完整 URL token。

## 12. 存储与迁移

建议新增迁移 `018_report_workbench.sql`，所有表带 `tenant_id` 并启用 `ENABLE/FORCE ROW LEVEL SECURITY`。表结构最小集合如下：

### 12.1 `report_jobs`

保存 `id/tenant_id/user_id/prompt/plan_json/plan_hash/execution_mode/frequency/timezone/max_runs/runs_started/schedule_end_at/status/next_run_at/page_ttl_days/permanent/created_at/updated_at`。不保存原始权限快照；执行时重新读取当前权限。

### 12.2 `report_runs`

保存 `id/job_id/tenant_id/user_id/status/attempt/run_key/started_at/finished_at/last_node/query_count/row_count/token_usage/partial/error_code/internal_error_message/user_error_message/trace_id`。内部错误只进入受控日志和管理员排障接口，`user_error_message` 用于用户界面；`run_key` 唯一，保证定时认领和手动重试幂等。

### 12.3 `report_run_steps`

保存 `id/run_id/seq/node/status/attempt/input_hash/output_ref/started_at/finished_at/duration_ms/error_code/internal_error_message/user_error_message`。`output_ref` 只能指向内部受控对象或摘要，不允许直接保存凭证和未脱敏结果。

### 12.4 `report_versions`

保存 `id/report_id/job_id/run_id/version/document_json/data_manifest_json/checksum/expires_at/is_latest/created_at/deleted_at`。页面版本写入后不可更新；设置最新版本通过事务切换 `is_latest`。

### 12.5 `report_shares`

保存 `id/report_version_id/report_id/tenant_id/created_by/token_hash/code_hash/scope/user_scope/user_ids/allow_drilldown/allow_download/pin_latest/expires_at/revoked_at/view_count/last_viewed_at`。`token_hash` 唯一；只允许通过受控路由查询公开文档。`user_scope=specified` 时只允许 `user_ids` 中的登录用户访问，`user_scope=tenant` 由租户策略控制。

### 12.6 `report_quota_policies` 与 `report_quota_usage`

前者保存租户默认值和用户覆盖值，后者按 `tenant_id/user_id/period_start/period_end` 保存原子计数、Token、查询、并发和分享数。RLS 和管理员查询必须明确区分“查看本人用量”和“管理本租户用量”。

### 12.7 访问审计与清理任务

分享访问、授权码解锁、私有/公开 Drilldown、分享创建和撤销必须写入 `report_access_audit`（或统一审计通道），只保存租户、用户、分享/报表 ID、动作、结果、IP 摘要和有界元数据，不保存完整 token、授权码、SQL 或 Prompt。

新增 `ReportRetentionService`：每轮最多处理固定批次；先撤销过期 share，再软删 version，再清理对象数据。清理失败指数退避并保留 `retention_error` 审计；不得因清理失败阻塞新的报表运行。

## 13. 重试、失败和恢复

### 13.1 错误分类

| 类别 | 示例 | 策略 |
|---|---|---|
| `validation` | 计划 JSON、页面 DSL、图表 option 不合法 | 不自动重试；执行结构化修复一次，仍失败则终止。 |
| `permission` | 数据源撤权、列/行策略拒绝 | 不重试；记录当前权限快照摘要并提示用户重新授权。 |
| `security` | 非只读 SQL、危险函数、越权过滤 | 不重试，触发审计告警。 |
| `transient_database` | 连接断开、超时、临时限流 | 指数退避最多 2 次，单元隔离。 |
| `llm_transient` | Provider 429/5xx、超时 | 最多 3 次，遵守 Retry-After 和 Token 配额。 |
| `llm_contract` | 结构化输出解析失败 | 同一模型修复提示 1 次，仍失败可切换租户配置的降级模型 1 次。 |
| `quota` | 周期 Token/次数/并发超限 | 不重试，等待下一周期或管理员提升额度。 |
| `rendering` | DSL 组件/数据超过限制 | 先确定性降级（减少样本、隐藏非核心块），失败后标记部分成功。 |
| `internal` | 未分类异常 | 不向用户暴露堆栈；记录 trace_id，允许管理员手动重试。 |

### 13.2 部分成功

单个分析单元失败时，如果至少一个主单元成功，运行状态为 `partial`，页面仍可发布，但必须包含失败单元的 `callout` 和重试入口；若所有主单元失败，不发布可访问页面。联合分析依赖的任一单元失败时，联合区段标记 `blocked`，不能用剩余数据猜测关系。

### 13.3 取消和恢复

- 用户取消通过 `run_id` 传播到当前 LLM、数据库 Connector 和对象存储写入；取消后不发布新版本。
- 服务重启后由 `report_runs.status in ('queued','running')` 和心跳超时扫描恢复；步骤以 `(run_id,node,attempt)` 幂等重放。
- 定时任务使用 PostgreSQL `FOR UPDATE SKIP LOCKED`，认领时推进 `next_run_at`；达到 `max_runs` 后自动停用。SQLite 测试和轻量部署不使用该语法，由 `ReportStore(dialect="sqlite")` 在事务外使用应用层互斥锁和条件更新模拟认领；不能假设 SQLite 具备 PostgreSQL RLS。
- 手动重试创建新的 `attempt`，复用成功的不可变分析单元结果时必须校验结果版本和权限未过期；默认只重跑失败单元。

## 14. 前端工作台设计

### 14.1 页面入口

将现有“自动化”入口升级为“报表工作台”，旧自动化任务放入“兼容任务”折叠区。菜单由 `report.menu` 控制；无权限用户不显示入口，也不能通过 URL 绕过。

### 14.2 创建区

首屏是自然语言输入框和授权数据源多选；点击“生成计划”后显示：

- 执行方式、频率、次数、时区和页面有效期；
- 识别出的业务数据单元和预计查询/Token/耗时；
- 数据源权限提示、潜在关联和不确定项；
- 立即执行、确认定时、修改计划三个动作。

不把 SQL 输入作为主要入口。高级用户可展开“查看生成 SQL/证据”，但只能读，不能把任意客户端 SQL 注入报表计划。

### 14.3 报表查看器

查看器采用“页面标题 + 有限状态栏 + 动态区段 + 运行日志抽屉”布局：

- 顶部展示生成时间、有效期、版本、数据范围和权限标识；
- 侧栏按区段导航，移动端改为顶部 Tabs；
- 页面块按 DSL 的 Grid 位置渲染，保持稳定宽高，图表、表格和长文本不互相遮挡；
- 页面底部集中展示来源、数据质量、限制和生成运行信息；
- 部分成功、过期、权限撤销和重试状态必须有明确横幅，不显示空白欢迎页。

### 14.4 运行日志

运行抽屉按时间线显示节点、耗时、重试次数、状态、用户可读错误和 trace_id；管理员可展开脱敏诊断字段。普通用户看不到 Prompt、Token 原文、连接串和完整 SQL 参数。

### 14.5 分享和有效期操作

页面工具栏提供“延长 7/30 天”“设置永久（需权限）”“创建分享”“管理分享”“重新运行”。创建分享弹窗明确显示：固定版本/最新版本、是否需要授权码、是否允许登录用户钻取、是否允许下载和过期时间。

## 15. 安全与隐私

1. 数据源、知识库、Skill 和页面访问始终按当前租户和用户授权；旧 checkpoint 或旧任务不能恢复过期权限。
2. 每条报表 SQL 走现有 `validate_sql`、`validate_and_execute_sql`、EXPLAIN、列白名单、行过滤、限流、脱敏和审计链路。
3. 页面 DSL 只允许纯数据；Markdown/富文本通过安全渲染器处理，禁止原始 HTML、脚本、事件属性和外链资源。
4. 已审核知识库可作为可信业务口径；附件、网页和未来联网内容仍按不可信外部数据处理，内容不能改变安全、权限、只读或工具授权。
5. 分享快照只保留脱敏后字段和值；来源只显示允许的名称和摘要，不显示内部表结构、SQL 或权限规则。
6. 公开接口按 token、IP、授权码失败次数和租户策略限流；所有访问写审计但不记录完整 token。
7. 下载默认关闭；开启时只能导出当前版本允许字段，并在审计中记录 share_id、用户/IP、行数和字节数。
8. 租户停用、用户停用、数据源删除或权限撤销时，私有页面可按管理员策略保留历史，但 live drilldown 和分享立即失效。

## 16. 可观测性与运行指标

重要边界使用 structlog：计划解析入口/完成、配额预占/释放、任务创建、调度认领、运行开始/成功/失败、节点重试、版本发布、分享创建/访问/撤销、过期清理。日志不得包含密码、token、Cookie、连接串、完整授权码或完整行数据。

建议指标：

- `report_plan_total{mode,result}`、`report_run_total{status,error_code}`；
- `report_run_duration_ms{node}`、`report_query_count`、`report_partial_rate`；
- `report_llm_tokens{tenant_id,model}`、`report_quota_rejection_total`；
- `report_share_access_total{scope,result}`、`report_drilldown_total{mode,result}`；
- `report_expired_cleanup_total{result}`。

运行日志文件继续使用七天 `TimedRotatingFileHandler`，敏感字段沿用现有脱敏处理器。

## 17. 配置项建议

```text
report_workbench_enabled=true
report_default_ttl_days=7
report_max_ttl_days=90
report_default_timezone=Asia/Shanghai
report_max_units_per_run=12
report_max_queries_per_run=16
report_max_concurrency=4
report_max_page_bytes=2097152
report_max_public_share_days=30
report_public_share_enabled=true
report_public_rate_limit_per_minute=60
report_retry_max_attempts=3
report_run_timeout_seconds=900
report_retention_interval_seconds=3600
report_default_monthly_jobs=20
report_default_monthly_runs=50
report_default_monthly_tokens=200000
```

所有默认值都可被租户策略收紧，不能被普通用户请求体放宽。`permanent` 不是绕过租户保留策略的开关。

## 18. 实现分期与兼容迁移

### Phase 1：立即报表闭环（P1）

1. `ReportPlan` 规则/LLM 解析、预览和立即执行一次。
2. `report_workbench_subgraph` 的单数据源、多分析单元、动态页面 DSL 和版本存储。
3. 基本失败状态、节点日志、三次结构化重试和七天过期。
4. 私有查看器、快照 Drilldown、创建/撤销分享（授权码可选）。
5. 每用户菜单权限、周期次数和 Token 配额。

### Phase 2：调度和联合分析（P1/P2）

1. 每小时/日/周/月、运行次数和结束日期、定时认领、暂停/恢复。
2. 多数据源隔离执行、JoinContract 关系证据和跨业务综合分析。
3. 登录用户 live drilldown、下载权限、版本指针和延长/永久策略。
4. 管理员租户配额控制台、成本和失败趋势。

### Phase 3：生产化增强（P2）

1. 对象存储、冷热数据和批量清理；大报表渐进加载。
2. 失败单元级重试、模型降级链、成本预测和预算预警。
3. 报表模板/主题扩展、审阅和发布流程、审计导出。

兼容策略：

- 旧 `/automation/schedules` 接口和旧 `AutomationPage` 至少保留一个迁移周期；旧任务不自动转换为新 DSL。
- 新工作台可只读展示旧任务和运行记录，用户点击“升级为报表任务”时用 SQL 作为一个 `AnalysisUnit` 生成新 Job。
- 新接口使用新的表和权限键，不复用旧 `analysis_schedules.kind` 的枚举；旧通知仍由原服务投递。

## 19. 测试与验收

### 19.1 计划解析

- 无调度词的“生成销售报表”默认 `immediate + max_runs=1`；
- “每周一 9 点执行 4 次”正确解析时区、频率、次数；
- 冲突时间/次数返回 `needs_confirmation`，不创建任务；
- 未授权数据源、永久权限、超过 Token 预算在计划阶段拒绝；
- 规则优先级覆盖 LLM 误判，真实模型测试使用 Fake LLM。

### 19.2 图编排与分析

- 多个分析单元并行执行且单元失败隔离；
- 同一表同一粒度缺口合并查询，不产生重复补查；
- JoinContract 匹配率、基数和类型不满足时不联合；
- 跨源列权限和行过滤在每个单元重新注入；
- 相关性报告带 evidence level 和 limitations，不断言因果；
- 单值、时间序列、分类、混合业务数据均生成合适图表和区段。

### 19.3 页面与交互

- DSL 非法组件、脚本、超长文本、外链和函数 option 被拒绝；
- 桌面、平板、移动断点不重叠，空数据和部分失败有可见状态；
- snapshot drilldown 只能返回当前版本允许字段；
- live drilldown 在登录用户撤权后立即 403，公开链接永远不能 live；
- 图表点击只提交 `drilldown_id + filters`，服务端不接受客户端 SQL。

### 19.4 生命周期与分享

- 默认七天到期、延期、永久权限和到期清理正确；
- token 只返回一次，数据库/日志无明文 token/授权码；
- 撤销、过期、用户停用、租户停用立即阻断访问；
- 固定版本分享不随新运行改变，latest 分享按策略更新；
- 公开访问不泄漏 SQL、Schema、知识原文、权限规则或未脱敏字段。

### 19.5 配额、重试和恢复

- 并发预占在多 Worker 下不超额；周期边界按租户时区正确归属；
- 节点可重试错误按上限退避，权限/安全/配额错误不重试；
- 服务重启可恢复心跳超时运行且不重复发布版本；
- 运行失败、部分成功、取消和手动重试均有步骤日志和用户可读错误；
- 每个公开方法至少覆盖正常、边界、异常和回退路径，测试默认 Mock LLM。

### 19.6 验收场景

1. 输入“分析销售、库存和客户留存，立即生成报表”后生成一个包含三个独立区段的页面，发现可靠关系时追加关系区段，发现不了时明确说明。
2. 输入“每周一九点执行四次”后只创建定时任务，不立即额外执行；运行四次后自动停用。
3. 从报表页面点击销售图表数据点，只返回允许字段的脱敏明细；更换过滤条件不能访问未签发字段。
4. 分享链接无授权码可直接访问快照；带授权码的链接错误多次被限流；撤销后立即失效。
5. 用户没有 `report.menu` 时菜单和所有私有 API 均不可用；管理员可为用户设置每月 3 次和 20,000 Token 上限。
6. 某数据源权限在任务创建后被撤销，下一次运行失败并记录 `PERMISSION_REVOKED`，不投递陈旧页面。

## 20. 风险与决策记录

| 风险 | 处理决定 |
|---|---|
| “发散分析”导致 Token 和查询爆炸 | 计划阶段估算并限额；分析单元、查询数、并发和页面块全部有上限。 |
| LLM 生成看似合理但无证据的跨业务结论 | 所有联合结论必须引用 `RelationEvidence`；无证据只能写成待验证假设。 |
| 动态页面被注入脚本 | 页面只接受版本化 DSL，前端组件白名单，禁止 HTML/函数/外链。 |
| 公开链接泄漏业务数据 | 固定脱敏快照、可选授权码、短期 token、限流和撤销；公开禁用实时钻取。 |
| 旧自动化迁移造成行为变化 | 旧表/接口保留兼容层，新工作台独立存储和权限键，显式升级而非隐式迁移。 |
| 页面到期但分享缓存仍可访问 | 分享和版本都校验 `expires_at/revoked_at`，响应 `no-store`，清理任务只做补偿。 |

## 21. 租户额度管理接口与可靠性落实（2026-09-06）

本节落实 §10 已定义的租户管理员职责，不把平台超级管理员作为租户日常业务额度的配置入口。当前统计周期仍为自然月，而不是自然周；任务创建次数和实际运行次数分别展示。

| 方法 | 路径 | 契约 |
|---|---|---|
| GET | `/api/v1/report-workbench/admin/users/{user_id}/policy` | 仅 tenant_admin；返回目标用户有效策略、能力、自然月标识和余额；目标必须属于当前租户 |
| PUT | `/api/v1/report-workbench/admin/users/{user_id}/policy` | 仅 tenant_admin；完整替换目标策略与能力；数据库事务成功后更新运行缓存，不清空已用量 |

PUT 请求示例：

```json
{
  "capabilities": ["report.menu", "report.create"],
  "max_jobs_created": 30,
  "max_runs_started": 50,
  "max_concurrent_runs": 2,
  "max_analysis_units_per_run": 12,
  "max_queries_per_run": 16,
  "max_rows_per_query": 100000,
  "max_llm_tokens": 200000,
  "max_page_bytes": 2097152,
  "max_share_links": 20
}
```

响应包含 `user_id`、`capabilities` 和 `policy`，GET 另含 `quota` 与 `period="calendar_month"`。缺少租户管理员角色返回 403；目标不存在或跨租户返回 404；非法用户 ID 或配额值返回 422。PUT 未提供的字段使用模型默认值，不是局部 PATCH。

创建任务采用同一身份、同一幂等键的事务锁；事务内完成去重、月度任务额度预占和 Job 写入，只有首次创建才入队。单次单元、查询和行数限制不再与月度累计用量比较。取消通过 CancelledError 传播并落库，最终清理只释放当前运行实际取得的槽位，跨月时使用预占周期。每个报表运行与分析单元使用独立线程标识。

本节不表示 §18 的定时调度、实时钻取、页面延期和生产恢复扫描已经实现，完成度以 `features/23-report-workbench.md` 的独立条目为准。
