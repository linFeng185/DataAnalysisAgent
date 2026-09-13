# 23. 报表工作台

本模块对应 `spec/30-report-workbench.md`。2026-09-06 续开发完成持久化调度、实时钻取、页面延期与有界跨源样本聚合。旧 `analysis_schedules` 保留兼容，生产 PostgreSQL 多 Worker 与真实模型验收另行执行。

| # | 功能 | 主要文件/模块 | 描述 | Phase | 状态 | 优先级 |
|---|---|---|---|---|---|---|
| 23.1 | 自然语言计划解析 | `src/reporting/planner.py`、`src/api/routes/report_workbench.py`、`src/llm/prompts.py` | 从文本解析立即/定时、频率、次数、有效期、数据源和分析单元；缺少调度信息默认立即一次。业务语义拆解走 `report.plan` 结构化 LLM（含租户模型绑定；用户书写的编号章节一个章节一个单元），模型不可用或越权时回退规则拆分并在 `warnings` 标注降级。分析单元默认不绑定数据源，模型在授权集合内的猜测会被清空，只有用户原文点名的数据源才保留 | Phase 1 | 单测完成 | P1 |
| 23.2 | 报表任务与运行模型 | `src/reporting/models.py`、`src/reporting/store.py`、`migrations/018_report_workbench.sql` | Job/Run/Step/Version 持久化、幂等、状态机和 RLS；SQLite 测试走应用层模拟 | Phase 1 | 单测完成 | P1 |
| 23.3 | 报表 LangGraph 子图 | `src/graph/subgraphs/report_workbench.py`、`src/graph/contracts.py` | 单数据源多单元拆解、授权 Schema、查询执行、确定性分析、综合洞察和页面发布。执行前由 `report.route` 按各源表清单判定单元命中的数据源，只打命中源；无命中则跳过并说明，不向无关数据源广播 | Phase 1 | 单测完成 | P1 |
| 23.4 | 多业务关系分析 | `src/reporting/relations.py`、`src/graph/subgraphs/report_workbench.py` | 受控列投影、样本限制、类型/匹配率/基数校验、连接膨胀阻断和跨源降级；只发布可验证关系证据，不宣称因果关系 | Phase 2 | 集成测试完成 | P1 |
| 23.5 | 动态报表页面 DSL | `src/reporting/document.py`、`frontend/src/components/report/` | 版本化页面 JSON、响应式布局、图表/表格/洞察/质量块和 ECharts 白名单校验 | Phase 1 | 单测完成 | P1 |
| 23.6 | 交互明细与 Drilldown | `src/reporting/drilldown.py`、`src/api/routes/report_workbench.py` | 私有/分享快照钻取；客户端不能提交任意 SQL，live drilldown 在 Phase 2 | Phase 1 | 单测完成 | P1 |
| 23.7 | 生命周期与分享 | `src/reporting/sharing.py`、`src/api/routes/report_workbench.py` | 默认 7 天、快照分享、指定用户范围、授权码和撤销；延期/永久策略在 Phase 2 | Phase 1 | 单测完成 | P1 |
| 23.8 | 菜单权限与用户配额 | `src/reporting/quota.py`、`src/api/routes/report_workbench.py` | `report.*` 能力、周期次数、并发、查询、Token 和分享限额 | Phase 1 | 单测完成 | P1 |
| 23.9 | 工作台与公开查看器 | `frontend/src/pages/ReportWorkbenchPage.tsx`、`frontend/src/pages/ReportViewerPage.tsx`、`frontend/src/pages/SharedReportPage.tsx` | 自然语言计划预览、任务轮询、节点日志抽屉、动态 DSL 阅读、快照 Drilldown、授权码分享和撤销 | Phase 1 | 单测完成 | P1 |
| 23.10 | 重试、恢复与可观测性 | `src/reporting/runner.py`、`src/reporting/store.py` | 基础节点日志、失败状态、用户/内部错误分离和部分成功；节点重试、恢复扫描在 Phase 2/3 | Phase 1 | 单测完成 | P1 |
| 23.11 | 定时认领与恢复扫描 | `src/reporting/scheduler.py`、Store、应用生命周期 | 日历/时区/次数/截止时间、暂停恢复、SKIP LOCKED 与专属连接锁、心跳及原周期配额恢复；发布前重新校验权限 | Phase 2 | 单测完成 | P1 |
| 23.12 | 登录用户实时 Drilldown | `src/reporting/live_drilldown.py`、私有模板表、API、查看器 | 服务端模板、当前 ACL、字面量过滤、EXPLAIN、限流、配额、脱敏；公开分享不下发 live 定义 | Phase 2 | 集成测试完成 | P1 |
| 23.13 | 页面延期与永久策略管理 | 报表 Store、API、查看器 | 已过期但未清理页面可延期；每次最多 30 天；永久能力独立校验；旧分享不自动续期或恢复 | Phase 2 | 集成测试完成 | P1 |
| 23.14 | 跨源聚合分析完善 | `src/reporting/aggregation.py`、报表子图 | 明确共同粒度、独立来源样本统计、行数/分组/字节预算与安全降级；不冒充全量业务总额 | Phase 2/3 | 集成测试完成 | P2 |
| 23.15 | 报表视觉模板与生成期选择 | `migrations/027_report_job_template.sql`、`src/reporting/models.py`、`src/reporting/store.py`、`src/reporting/runner.py`、`src/reporting/document.py`、`src/api/routes/report_workbench.py`、`frontend/src/report/templates.ts`、`frontend/src/components/report/TemplateScope.tsx`、`frontend/src/components/report/chartBase.ts`、`frontend/src/hooks/useReportTemplate.ts` | 四套内置模板（洞察/墨玉/纸感/商务）的配色·字体·密度 token 体系；模板在创建任务时选定并存入 `report_jobs.template_id`（**不进 `ReportPlan`、不进 `canonical_hash`**，故换模板不触发重新预览与重新确认），发布时写入 `document.theme.template_id`；阅读端与分享页可临时切换而不改版本；CSS 变量 + 嵌套 antd ConfigProvider + ECharts 基线三面注入，作用域限定报表容器。前端为视觉唯一真源，后端只校格式不持清单。设计见 `spec/41-report-visual-templates.md` | Phase 3 | 单测完成 | P2 |
| 23.16 | 确定性深挖分析 | `src/reporting/profiling.py`（适配层）、`src/tools/processors.py`、`src/graph/subgraphs/report_workbench.py`、`src/reporting/document.py`、`frontend/src/components/report/ReportDocumentRenderer.tsx` | **复用** `src/tools/processors.py` 既有统计能力（环比/同比/趋势/异常/贡献度/分布），报表侧适配层只做参数推导与 `ProcessorResult → UnitFinding` 转换，**不重写统计逻辑**；补报表级严谨性：IQR 1.5 倍 + MAD 修正 z-score 双判据、R² < 0.3 不给方向性措辞、样本截断披露、`method` 必填。时间列识别改用**列值可解析性**（≥80% 取值能解析成日期）而非列名或 schema 类型——`resolved_units` 只带列名不带类型，而列值判据比命名启发更强。新增节点 `report_analyze_units` 与新 block 类型 `findings`。**同时修 `processors.yoy` 的既有 bug**（其 `process()` 与 `mom` 逐字相同，算的是环比却输出"同比均值"）| Phase 3 | 单测完成 | P1 |
| 23.17 | 两阶段发散与档位额度 | `src/reporting/models.py`、`src/reporting/planner.py`、`src/llm/prompts.py`、`src/llm/output_contracts.py`、`src/graph/subgraphs/report_workbench.py`、`src/reporting/runner.py`、`src/api/routes/report_workbench.py`、`frontend/src/pages/ReportWorkbenchPage.tsx` | 计划期发散（`report.diverge`，schema 驱动，只看得到表名清单）与运行期发散（`report.supplement` + `report_decide_supplement` 受控回路，数据驱动，基于实际取回数据算出的显著发现决定补查）两阶段。档位是**一次性总授权额度**（保守 0 / 标准 3 / 深度 6）而非每轮授权，两阶段共享同一份额度；回路轮数 0/1/2、单轮 0/2/3。回路进入前走确定性前置筛选（额度未用尽 + 轮次未到 + 单元数 < 12 + 至少一条 high/moderate 发现），不满足则不消耗 LLM 调用。服务端重分配 `unit_id`、清空越权数据源、硬校验因果措辞、运行期建议必须带 `supports_finding`；优先级固定 secondary、时间范围继承主单元。回路**只执行新增单元**（执行逻辑抽为共享 helper，避免重跑已有单元导致成本倍增）。`runner` 按档位总额度做最坏情况预占，页面 `limitations` 强制披露运行期追加数量 | Phase 3 | 单测完成 | P1 |
| 23.18 | 跨源分布级关联与影响判定 | `src/reporting/relations.py`、`src/reporting/document.py`、`frontend/src/components/report/ReportDocumentRenderer.tsx` | 跨源改为**两级证据**：① 先走既有受控键匹配（`analyze_same_source` 在跨源且有样本行时本就能给出 `verified_relation` 与匹配率，证据更强）；② 键匹配降级为 `no_relation`/`unsupported_cross_source` **且两侧都有样本行**时才退回新增的分布级秩相关（Spearman）；③ 有一侧无样本行则保留原有结构证据。分布级对比覆盖的是"有共同指标但无可用连接键"的场景（如两侧时间列异名），**不是所有跨源**。分布级路径按周期标签文本精确匹配对齐（比日期运算更保守），无时间列时比较分布形态并声明未对齐；`join_keys` 与 `match_rate` 保持空值（跨源没做连接，填进去就是伪造），对比指标写进 `limitations` 文本；强制附"不构成因果"。关系块派生 direction/strength/cross_source 展示，证据层不存冗余字段。**落地时修正了原设计**：原稿写"跨源不做键匹配"是错的，按此实现会用弱证据替换强证据，5 个既有用例当场失败 | Phase 3 | 单测完成 | P1 |
| 23.19 | 指标口径溯源 | `src/reporting/runner.py`、`src/reporting/models.py`、`src/graph/subgraphs/report_workbench.py`、`frontend/src/components/report/ReportDocumentRenderer.tsx` | 把单元问题里 `@metric(...)` 引用解析出的**已批准口径**呈现在页面上，产出 `source_citation` block（该类型此前"定义了无产出"，本项填上）。**实现比原设计更简**：口径解析已由既有工作流在 `retrieve_schema`（`retrieve_schema.py:147-157`）内完成并写入 state 的 `activated_knowledge`，报表侧只需按 `source_id` 的 `metric:` 前缀筛出来透传，**零额外 LLM 调用、零额外查询**。原设计写的"按 `AnalysisUnit.metrics` 名称与别名精确匹配"是错的——既有机制解析的是问题文本里的 `@metric(code)` 引用，不存在名称匹配。**不做数值对照**（指标目录无目标值字段，只有 code/name/datasource/dialect/expression/description/unit）；不解析 `description` 自由文本；无引用即不产出区段。装饰性来源块构建失败时降级为不展示并记 warning，不让整份报表失败 | Phase 3 | 单测完成 | P2 |
| 23.20 | 预览裁剪与计划单调删减 | `src/reporting/models.py`、`src/reporting/store.py`、`src/api/routes/report_workbench.py`、`migrations/028_report_job_preview_hash.sql` | `POST /jobs` 新增 `accepted_unit_ids`（`None` = 全部接受，`[]` = 422），服务端校验其为预览计划 `unit_ids` 的子集后重建 effective plan（重新走全部既有校验）；**只允许删减不允许新增**，故不构成越权；被删 id 记入 `ReportPlan.excluded_unit_ids` 保留审计线索；`plan_json` 存 effective、`plan_hash` 语义不变、新增 `preview_hash` 存审计凭证；与 `plan_override` 互斥。裁剪只作用于计划期单元，运行期追加由档位额度约束。裁剪逻辑抽为 `_apply_unit_trimming` 便于单测；`ReportJobRecord` 新增 `plan_hash` 与 `preview_hash` 供审计；前端在建议单元上加勾选框，未取消时传 `null` 走全部接受。设计见 `spec/42` 第 9 节 | Phase 3 | 单测完成 | P1 |
| 23.21 | 报表运行计数统计 | `src/reporting/run_stats.py`、`src/llm/client.py`、`src/security/sql_execution.py`、`src/reporting/runner.py`、`src/reporting/store.py`、`src/reporting/models.py`、`migrations/029_report_run_stats.sql`、`frontend/src/pages/ReportWorkbenchPage.tsx` | 修复既有缺陷：`report_runs` 的 `query_count`/`token_usage` 写入的是 `plan.estimated` 的**估算值**而非实际发生值。新增五列真实计数 `llm_call_count`/`llm_failure_count`/`sql_query_count`/`sql_success_count`/`sql_failure_count`。用 contextvar 持有**可变**计数器对象（asyncio task 复制上下文后仍共享同一引用，若存不可变 int 会丢失子任务计数，这是必须避开的陷阱）。LLM 埋点在 `src/llm/client.py` 工厂层——唯一全覆盖层，`generate_sql` 等直连 `get_task_llm` 不走 `invocation.py`；SQL 埋点在 `validate_and_execute_sql()` 这一唯一执行入口。非报表路径无活动计数器时埋点为 no-op。既有 `query_count` 配额语义**保持不变**，界面可对比展示预估 vs 实际。设计见 `spec/43-report-run-statistics.md` | Phase 3 | 单测完成 | P2 |
| 23.22 | 报表字段备注与生成字段描述 | `src/llm/output_contracts.py`、`src/llm/prompts.py`、`src/reporting/models.py`、`src/reporting/runner.py`、`src/reporting/document.py`、`frontend/src/components/report/ReportDocumentRenderer.tsx` | 表格列标题与 KPI 标题改为「列名（说明）」。两个来源都在既有链路里，**零新增查询与 LLM 调用**：数据源列备注取自旧工作流 state 的 `relevant_tables`（`retrieve_schema.py:179` 本就在输出 `columns[].comment`，此前被丢弃）；自生成字段说明由 `AnalysisOutput` 新增 `field_descriptions` 承载（该节点本就按单元执行）。优先级：**数据源备注 > 模型说明 > 原列名**，都没则不凭空补。关键约束：`content.columns` 必须保持**原列名**（前端 `dataIndex` 依赖它取值），显示标题走新增的 `content.column_labels` 映射。设计见 `spec/42` 第 18 节 | Phase 3 | 单测完成 | P2 |

## 依赖关系

```text
11 API / 12 Security / 13 Analytics / 14 Visualization
             ├──────────────┐
             ▼              ▼
       23.1 Planner ──→ 23.2 Job/Run Store
                              │
                              ▼
                      23.3 Report Subgraph
                       ├──→ 23.4 Relations
                       ├──→ 23.5 Document DSL
                       ├──→ 23.6 Drilldown
                       └──→ 23.7 Lifecycle/Sharing
                              │
             21 Platform Admin ──→ 23.8 Quota/Permission
                              │
                              ▼
                      23.9 Frontend / 23.10 Operations
```

## 模块收尾

模块总计 22 项，已完成 22 项，待开发 0 项。这里的完成指当前编号描述的实现与测试状态，不表示真实 PostgreSQL、容灾演练或外部模型的生产验收完成。

| 待开发项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 23.18 跨源分布级关联与影响判定 | 同 23.16；设计见 `spec/42` 第 7 节 | 无外部依赖；真实跨源样本可用测试数据构造 | Phase 3 |
| （阶段 A 前置修复）**已完成** | 未单列功能点，属 23.16/23.17 的实施前置，四项均已落地：① `runner._default_execute_unit` 注入 `suppress_supplemental_query=True`，并在 `after_analyze_result` 条件上追加同名否定项关闭旧补查回环；② 修 `processors.yoy` 既有 bug（原先与 `mom` 逐字相同、算的是环比却输出同比，现改为按年平移对齐去年同期）；③ `UnitResult.truncated` 透传旧工作流已有信号（**未新增 `total_rows`**：原始行数在现有链路不可得，为拿不到的分母下钻 `sql_execution.py` 与 `execute_sql.py` 不划算，且这两个文件正被另一条并行开发线修改）；④ `EstimatedResources.query_count` 上限 16→64（18 个单元 × 平均 4 源） | — | Phase 3，已完成 |
| （其余项） | 本轮编号范围内均已实现 | 生产验收见下文，不另伪造开发完成状态 | — |

### 2026-09-13 配额层级与预占修复批次

- **预占重复计算共享额度**：`_reserve_plan` 按 `divergence_unit_budget(level) × 数据源数` 全量预留运行期发散额度，**没有扣减计划期已经用掉的部分**（深度档 6 × 5 个源 = 凭空多 30 次）。子图的 `_remaining_budget` 早就做了扣减，两处口径不一致。已改为同样扣减，与 `spec/42` 第 4 节"档位是两阶段共享总授权"的不变量对齐。
- **系统上限从未生效**：`report_max_queries_per_run` 等配置项（`config.py`）此前**除定义处外零引用**，`set_user_policy` 不做任何越界校验，因此"租户管理员的用户上限不得超过超级管理员配置的系统上限"这条规则**根本没有实现**。已新增 `_system_quota_limits()` / `_validate_within_system_limits()`：按配置校验并返回 422，同时通过 `GET .../policy` 的 `limits` 字段下发，供前端表单与配置对齐。**一张 `_SYSTEM_LIMIT_FIELDS` 表同时驱动校验文案与下发字段**，避免两处脱钩。
- **额度表单硬编码**：`ReportPolicyEditor.tsx` 把 `max_queries_per_run` 的输入上限**写死为 16**，而系统配置是 100、后端契约允许 1000。结果租户管理员**在界面上根本无法配到系统上限**，实测用户账号的有效值正是被卡住的 16，真实扇出 17 因此被拒。已改为 `limits[key] ?? maximum`，以配置为准。
- **超额提示无数字**：`QuotaExceededError` 只带字段名，四处捕获点都返回固定文案（"本周期报表配额不足"），无法定位要调哪一项。已让异常携带 `field`/`limit`/`requested`（新参数为关键字可选，既有的位置参数构造与 `match="QUOTA_EXCEEDED"` 断言保持可用），并新增 `QUOTA_FIELD_LABELS` + `describe_quota_error()` 由 runner 与三个 API 捕获点共用，提示形如「单次查询上限为 16，本次任务需要 47，请联系租户管理员调整额度」。
- **模型自带约束卡住配置面**：`Settings.report_max_units_per_run` 的 Field 写死 `le=12`，而 `config/app.yaml` 与模板里已是 36；同类还有 `report_max_queries_per_run` 的旧上界 16。用户改配置后**服务在 `get_settings()` 阶段直接抛 ValidationError 退出**（不是静默忽略，`main.py` 连 Uvicorn 都没起来），现象被读成"改了没生效"。已把上界放开（12→200、16→1000）：该值由超级管理员按租户成本承受力设定，12/16 只是默认值，真实成本另受查询上限与 Token 预算约束。新增 `tests/test_config.py`：用 `config/app.yaml` 与模板的取值真的构造一次 `Settings`，任一越界即失败并打印字段名、实际值与 pydantic 原因；另断言该字段上界 ≥ 前端表单可提交值，且与 `reporting.models._UNIT_COUNT_STATIC_MAX` 一致。**已验证测试有效性**：把 `le` 注入回 12 时四个用例全红。

### 2026-09-13 首次真实运行修复批次

首次在真实环境跑通报表后暴露两个缺陷，均已修复并补上能真正抓住它们的回归：

- **`report.diverge` 模板变量缺失**：模板里有 `{limit}` 占位符，但调用方把它当 `PromptSection` 传，而 `PromptDefinition.render()` 走的是 `template.format(**values)`——变量必须经 `system_values` 传入。结果是每次计划期发散都在 render 阶段抛 `ValueError: 提示词 report.diverge 缺少变量: limit`，被降级捕获后只留一条 warning，**发散功能实际从未生效**。修复为传 `system_values={"limit": str(limit)}`，并新增 `TestPromptVariableContract`：用调用方实传的 `system_values` 真的渲染一次模板，缺变量即断言失败。
- **阶段 A 的旧回环修复是空操作**：原方案注入 `capability="report"`，但 `_infer_capability`（`contracts.py:262-292`）只按问题文本推断、不读预置值，注入当场被覆盖（真实日志仍是 `capability=sql_analysis`）。而它若真生效后果更严重——`after_analyze_result` 会把单元路由到 `report_subgraph`（生成报告而非取数）。已改为 `suppress_supplemental_query=True` + `after_analyze_result` 条件追加同名否定项。原测试只断言 state 字典的值，通过却无行为保证；现改为造齐回环全部前置条件、先断言不带标志时确实进入回环，再断言带标志被拦下。
- 同时把 `spec/30:265`、`spec/42` §11.1 与 §18、本文件阶段 A 行里"注入 capability 关闭回环"的失实描述改为真实机制。

**未修复、属设计范畴的观察**：本次报表全部分析失败的直接原因是**计划问题超出所选数据源的 Schema 能力**——单元问的是归因触点、发票差额、地区/渠道维度，而 oracle 数据源（143 张表，多为 `workforce_*`）没有这些字段。SQL 生成节点正确地返回 `needs_input` 并说明缺口，**系统没有幻觉造 SQL**，这是预期行为。但用户拿到的是"5 个空单元 + 状态 partial"的报表，计划阶段没有任何反馈。若要改善，需要在计划期做一次 Schema 可行性预检——属新功能，不在本批次范围。

### 2026-09-12 运行计数批次

- 23.21 修复既有缺陷：`report_runs` 的 `query_count` 与 `token_usage` 原先写入 `plan.estimated` 的**估算值**，界面显示的是计划预测而非实际发生值。新增五列真实计数并把 `token_usage` 改为真实 token 数；`query_count` 保持配额计费口径的估算语义不变。
- 必须应用迁移 `029_report_run_stats.sql`。**该迁移未执行到任何用户数据库**，本地与测试均走应用层模拟，真实 PostgreSQL 验收另行执行。迁移编号 027/028 由 `spec/41`（模板）与 `spec/42`（preview_hash）预留，本次刻意空号。
- 验证：`tests/test_reporting/test_run_stats.py` 14 例、`tests/test_reporting/` + `tests/test_observability.py` 119 例、`tests/test_api/test_grafana.py` + `tests/test_automation/test_runner.py` 13 例、`tests/test_security/`（排除另一并行任务在改的 `test_sql_schema_validation.py`）108 例全部通过；前端 `tsc -b` 通过。
- 未覆盖：真实 LangGraph 运行下的端到端计数（单元执行经 `app.ainvoke` 触发内部 task，contextvar 传播路径已有专门单测覆盖，但未做真机链路计数核对）；真实 PostgreSQL 上的迁移应用。

### 续开发交付与验收边界

- 新增迁移 `020_report_live_queries.sql`、`021_report_dispatch.sql`、`022_report_tenant_boundaries.sql`，本轮未主动执行到用户数据库。022 修复 018 中租户管理员的跨租户布尔分支，并把分享读取与写权限分离；标量 SQL 测试不等同于真实 PostgreSQL RLS 验收。
- 复现并修复私有过期钻取仍返回数据、手动重跑不复核 ACL、重复运行页面 ID 变化且版本始终为 1、已发布页面在状态回写前中断后重复执行等问题。
- 调度恢复使用新运行标识重跑中断任务，不复用未知权限的旧结果；节点级成功单元持久化缓存复用仍不作为本次验收承诺。进程内手动运行与后台扫描共享 PostgreSQL 专属连接锁，连接失效后旧 Worker 无法通过该连接发布。
- 23.14 只处理明确同粒度的授权样本，缺少维度、字段不可见、来源失败、类型冲突或超过预算时分别分析；没有实现无限制全量跨库 Join，也不宣称统计相关性代表因果。
- 验收前按顺序应用新迁移、安装 `vision` 可选依赖并给实际视觉模型声明 `vision=true`；使用真实 PostgreSQL 非 BYPASSRLS 角色演练跨租户、锁连接中断和多 Worker 认领。
- 历史批次记录如下，当前完成状态以上方总表为准。

### 最终本地验证（2026-09-06）

- `RUN_BROWSER_TESTS=1 pytest -m "not live_llm and not local_llm and not live_datasource"`：**1740 passed, 1 deselected, 1 warning**，含真实 Edge 隔离页面测试；不调用真实远程 LLM。
- 本轮所有变更 Python 文件 Ruff 通过；`compileall src tests` 通过；前端 TypeScript/Vite 构建通过。Antd 约 1.18 MB 体积告警保留，不通过提高阈值掩盖。
- 全项目 Ruff 另有 3 处未修改文件的历史导入排序问题：`src/api/routes/knowledge.py`、`src/memory/session_archive.py`、`tests/test_tools/test_db_executor.py`；本轮未夹带格式化。
- 独立内存 Uvicorn + `curl.exe` 验证：过期正文 GET 410、过期私有 Drilldown POST 410、延期 POST 200、再次读取正文 GET 200；完整日志显示 `expired=true` 时未进入数据返回。测试服务已关闭，没有操作用户业务数据。
- 真实 PostgreSQL 多 Worker、非 BYPASSRLS 角色、Vault 和视觉模型仍需独立环境验收。

## 2026-09-13 运行质量证据修复

- 对应既有 23.3、23.5、23.16，修复回归与子图集成测试通过；模块仍为 22 项已完成、0 项待开发，没有新增独立能力。
- 故障任务 `76198599-2fd9-4ff6-8512-a8225d598ee7` 的版本 1 中，数据质量章节是独立 `llm_direct` 结果，但运行记录实际有 12 次 SQL 执行成功。问题是说明单元未消费整轮运行证据，不是整张报表没有查询。
- 同时要求时间覆盖、截断、权限或未建立关系的报表说明单元，不再独立询问主数据源 Schema；在全部数据单元和运行期补查结束、关系证据建立后，确定性汇总样本日期范围、来源、截断、权限阻断及无法建立关系的原因。普通 Schema 说明和数据质量指标查询保留原路径。
- 说明单元不与数据查询按空维度/指标去重合并，先后顺序不再影响查询是否执行；成功说明不掩盖数据查询全部失败或授权阻断。文字结果不再追加“查询没有返回数据”的趋势告警。
- 保留分析结果的 `limitations`；多源 worker 的 50 行、合并的每源 30 行和总 200 行裁剪均透传截断标记，并在结果分析前传入；不把年月样本范围当作逐日全量覆盖，不把 Prompt 截断当作结果行截断。
- 验证：本轮两模块共新增 31 项回归；相关套件 505 通过、1 跳过，保留常量样本相关系数的既有警告。故障报表的 PostgreSQL 脱敏快照经新子图回放后，质量章节包含实际样本日期与 55 条关系的汇总，旧版本 id/checksum 未变；未调用远程模型、未写入替代版本。
- 验收边界：快照回放不等于远程 LLM 整份报表重新生成，也不等于浏览器登录态验收。当前后端在 PyCharm 调试器中运行，未启用热更新；需重启后端并重新运行报表，刷新既有版本不会自动替换错误正文。

## 2026-09-06 可靠性整改批次

- 23.2：创建任务、幂等检查和任务配额预占使用同一 PostgreSQL 事务；同身份同幂等键只首次入队，同键不同计划返回 409。手动重跑使用独立运行键，不再复用 `job_id:attempt`。
- 23.8：单次单元/查询/行数限制与月度用量分离；余额读取持久化记录；失败创建返还任务额度；取消释放原统计周期的并发槽。
- 23.8：租户管理员可在“租户管理 → 当前租户用户 → 报表额度”读取与配置能力和自然月额度；后端验证目标用户属于当前租户，策略和能力一起提交，成功后才更新缓存。平台超级管理员不能通过此租户业务端点代配。生产权限以持久化配置为准，跨 Worker 撤权不沿用旧覆盖；权限库异常时失败关闭，不恢复角色默认授权。
- 23.9：修复预览、创建、刷新及查看器重试按钮未调用函数的问题；失败重试沿用幂等键；明确区分每月任务额度和运行额度。新增真实浏览器请求拦截测试，不访问真实业务后端。
- 23.10：取消写入 `cancelled`；只有成功预占的运行才释放并发槽；报表会话以显式租户、用户及 run_id 隔离，避免依赖请求上下文或复用分析单元名。
- 验证：开启隔离浏览器用例、排除真实模型和数据源标记后的完整回归为 `1597 passed, 1 deselected`；前端构建通过，ECharts 图表相关代码由约 1.15 MB 降至约 0.74 MB。PostgreSQL 运行键使用 SQLite 唯一约束验证，不等同于真实 PostgreSQL 多实例验收。相关文件 Ruff 通过；全项目存在 5 处本轮未修改文件的历史导入排序问题，未夹带修改。
- 前序可靠性批次未覆盖 23.11～23.14；这些能力已在本页前述续开发批次实现，当前状态以总表为准。

## 2026-09-13 工作台拆分、后台计划、通知、链接管理与额度重置（需求 1～4）

本批次扩展 23.1、23.7、23.8、23.9 四项语义并新增 23.23～23.25；总表 23.1～23.22 的既有编号与状态不变。

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 23.23 | 计划后台生成与报表资源接口 | `src/reporting/plan_worker.py`、`src/reporting/store.py`、`src/api/routes/report_workbench.py`、`src/bootstrap.py`、`src/llm/prompts.py`、`src/llm/output_contracts.py`、`migrations/032_report_backend_planning.sql` | 点「生成计划」立即建记录（`planning`）并返回，后台复用既有 Planner 解析出计划与标题：成功置 `draft`（未生成）、失败置 `plan_failed` 且把可读原因写入 `plan_error`，两者都发个人通知；`report_jobs` 新增 `title`/`divergence`/`datasources_json`/`plan_error` 四列与创建时间索引，status 约束加 `plan_failed`。新接口 `POST/GET /reports`、`GET/PATCH/DELETE /reports/{id}`、`POST /reports/{id}/run|retry-plan`、`GET /reports/{id}/pages`；**点「生成计划」不预占额度，点「生成」才预占 `jobs_created`**，计划可改（只允许删减发散单元）；列表按创建时间倒序、租户管理员可看全租户并按用户筛选。旧 `POST /plans` 与 `POST /jobs` 保留为兼容入口 | 单测完成 |
| 23.24 | 链接管理与分享访问统计 | `src/reporting/share_analytics.py`、`src/reporting/store.py`、`src/api/routes/report_workbench.py`、`frontend/src/pages/LinkManagementPage.tsx`、`frontend/src/pages/SharedReportPage.tsx`、`migrations/030_report_share_analytics.sql` | 新增 `report_share_visits`（访客类型、身份、停留时长）与 `report_tenant_settings`（记录登录访客身份开关、每用户永久报表上限）。停留时长由前端埋点：只累计页面可见时间、每 30 秒以**累积值幂等**上报、卸载用 `fetch keepalive` 补发。租户内分享平铺列表（租户管理员可见全租户并可下架他人链接，其他角色仅自己创建）、最近访问的系统用户及停留时长、批量撤销、租户级设置读写；列表按系统用户与非系统用户**分列**访问量并给出总时长与平均时长 | 单测完成 |
| 23.25 | 额度已用量重置与永久页面上限 | `src/reporting/quota_reset.py`、`src/reporting/quota.py`、`src/reporting/store.py`、`src/api/routes/report_workbench.py`、`frontend/src/components/report/quotaResetFields.ts`、`ReportPolicyEditor.tsx`、`frontend/src/pages/AdminPage.tsx` | `POST /admin/quota-usage/reset` 支持单用户/多用户/全租户，`fields` 只能取 `jobs_created`/`runs_started`/`llm_tokens`/`share_links` 四个按月字段；只清当前周期用量、**不改上限**，字段白名单来自唯一常量，跨租户与本期无用量行不计入且不让整次请求失败，每个实际清零用户各写一条审计。延期与永久能力下放给 `analyst`；永久页面数受租户级 `max_permanent_reports_per_user` 约束（0 为不限制，租户管理员本人不受限） | 单测完成 |

本批迁移：`030`（分享访问与租户设置）、`032`（后台计划四列与状态约束）、`033`/`034`（通用通知三表、删除旧 `analysis_notifications`）。通用通知模块与「自动化」菜单摘除见 `features/18-frontend.md`。测试：`tests/test_reporting/test_backend_planning.py`、`test_share_analytics.py`、`test_quota_usage_reset.py`、`tests/test_notifications/`、`tests/test_frontend/test_report_pages.py`、`test_link_management_page.py`、`test_report_quota_reset.py`。
