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
| 23.23 | 报表呈现体系改版（hero · 无框 · 图文分栏） | `frontend/src/components/report/ReportHero.tsx`、`ReportBlockPair.tsx`、`ReportDocumentRenderer.tsx`、`frontend/src/report/templates.ts`、`frontend/src/index.css` | 正文去掉卡片墙（每块不再套 antd `Card`，回到 `spec/30:338` 的「区段无框」约束）；文档头升级为 hero 横幅（eyebrow + 标题 + 元信息 + KPI 概览），文字色按 `heroBg` 亮度推导复用既有 `isDarkSurface` 模式；按文档顺序将前 `HERO_KPI_LIMIT`(=4) 个 `kpi` 块提升进 hero 并从原 section 移除（每个 KPI 全页只出现一次）；**图文配对**：段内 chart 与 narrative 并存时按变体 1（62/38 左右分栏）呈现，图表为 `heatmap` 时走变体 2（图表满宽 + 下置研判带，限宽 72ch），配对容器占整行并显式接管两个块的 `col_span`（不生效时按后端原值渲染，行为不变）；区段标题去掉 3px 竖条改「编号 + 标题 + 副标题」；字形规则：表格列与坐标轴用 `tabular-nums`，hero/KPI 大数字用比例字形。新增 `sequentialPalette` 与 `heroBg` 两个 token（共 17 个）。设计见 `spec/49-report-presentation-redesign.md` | Phase 3 | 待开发 | P1 |
| 23.24 | 报表图表类型全覆盖（复用对话路径推断） | `src/tools/chart_generator.py`、`src/reporting/document.py` | 报表图表从写死的 `bar` 扩到对话可生成的全集 `line`/`bar`/`pie`/`scatter`/`heatmap`/`table`。**单一真源**：把 `_classify_chart_type` / `_build_option` 提升为公开的 `classify_chart_type` / `build_chart_option`，`ChartGeneratorTool` 与报表路径共用同一实现（实现不改，仅改可见性），消除两套并行实现必然分叉的问题。报表侧入参为已脱敏的 `safe_rows`；分类结果为 `table` 时建 `table` 块而非 `chart` 块（`_build_option` 对 table 返回的不是合法 ECharts option）；构建失败降级为只建 `table` 块并**强制写 `warnings`**（不静默）；图表标题不再硬编码「趋势与分布」，按类型与列语义生成。`_ECHARTS_ROOT_KEYS` 增加 `visualMap`（热力图必需，且是 `_build_option` 唯一产出的白名单外根键）；**值级约束不放宽**，仍经 `_validate_option_value` 递归拒绝函数/formatter/dataView/HTML。设计见 `spec/49` 第 4 节 | Phase 3 | 待开发 | P1 |
| 23.25 | 墨玉模板图表配色缺陷修复 | `frontend/src/report/templates.ts`、`frontend/src/components/report/chartBase.ts` | `ink` 模板的 `chartPalette` 8 色**全部超出深色亮度带**（实测 OKLab L 0.709–0.849，带内为 0.48–0.67），即深色底挂一排粉彩。替换为按暗色面验证过的 8 色（`#3987e5`/`#d95926`/`#199e70`/`#c98500`/`#d55181`/`#008300`/`#9085e9`/`#e66767`），8 色对比度全 ≥3:1。**槽序即色盲安全机制不可重排**。`accent` 不动（承载 KPI 数值与高亮，非序列色）。另补 `chartBase` 的单色阶梯注入（`visualMap` → `sequentialPalette`，`visualMap` 为数组时逐项处理）与 `tooltip.axisPointer` 十字准线。设计见 `spec/49` 第 7、8 节 | Phase 3 | 待开发 | P2 |
| 23.26 | 报表单元执行快照与单单元重跑 | `migrations/037_report_unit_snapshots.sql`、`src/reporting/snapshots.py`、`src/reporting/store.py`、`src/reporting/runner.py`、`src/api/routes/report_workbench.py`、`frontend/src/pages/ReportListPage.tsx` | 新增 `report_unit_snapshots`：每个单元一次执行一条，覆盖 `unit_id`、命中数据源与方言、执行的 SQL、渲染后的提示词与 prompt_id、模型标识、`params_json`、展示行数与查询全量行数、截断标记与可读原因、耗时与错误。`run_id + unit_id` 唯一，重跑覆盖同一条。RLS 按 022 的修正模式放行本人行（analyst 持 `report.create_page` 即可建报表，若策略只允许 tenant_admin，其快照写入会被 WITH CHECK 拒绝、功能静默失效）。单元执行结束前落快照，**写入失败只记 warning、不影响报表生成**；新增单单元重跑与快照读取端点，**重跑不自动重生成页面**（避免隐式副作用） | Phase 3 | 单测完成 | P1 |
| 23.27 | 单元时间范围抽取（需求 → 单元 → 查询） | `src/llm/output_contracts.py`、`src/llm/prompts.py`、`src/reporting/planner.py`、`src/reporting/runner.py` | 复现并修复事故：需求写「时间范围 2016-01-01 至 2036-12-31」，22 个单元的 `time_range` 全落回默认 `relative/90d`，90 天窗口只落回一个月，同比/环比全为 null。根因在契约层——`report.plan` 的输出契约与提示词里根本没有 `time_range` 字段，模型不知道要抽也没地方填。四层修复：契约加 `ReportPlanUnitTimeRange`（`relative`/`absolute`/`all`）；提示词交代按需求原话抽取（绝对区间 ISO 起止、相对区间 `90d`、需求没写就留空不编造）；规则层 `extract_time_range` 兜底（绝对区间 > 年度标题 > 相对区间，既有年度补丁并入）；查询拼装显式带回窗口。**套用位置必须在模型合并之后**（放构造计划之前会被模型给出的单元整批替换、修复本身失效，既有验收用例 `test_report_year_survives_unit_planning` 会挂）；模型给出的残缺 `time_range`（`None`、空串、非 ASCII 如「全部」）在规范化阶段丢弃并交回规则兜底——不得让一个残缺值使整个单元被契约拒收、业务问题一起静默消失 | Phase 3 | 单测完成 | P1 |
| 23.28 | 样本截断语义分离与阈值可配 | `src/config.py`、`src/graph/contracts.py`、`src/graph/nodes/execute_sql.py`、`src/graph/nodes/multi_source.py` | `sample_capped`（**给模型的提示词样本被裁**，提示词预算问题）与 `truncated`（真实执行截断）分离：`multi_source` 原先把 `len(data) > 50` 计入 `truncated`，让「共 60 行、全部展示」的单元被判成样本被截断。新增 `report_max_sample_rows`（默认 5000，`ge=100`/`le=100000`）替换 50/30/200 三处硬编码，`execute_sql` 回传、单源合并贡献与合并上限统一读它，配置读取失败回退历史默认值并记 warning。同时把「优先在数据库侧聚合（GROUP BY 与聚合函数）」前置进单元查询文本——行数超限的根因多是 SQL 没聚合，在源头提要求比事后截断更有效；该要求与时间范围提示是两段独立文本，不影响下游按「时间范围：」定位的解析 | Phase 3 | 单测完成 | P1 |
| 23.29 | 样本超限的模型处置建议 | `src/reporting/sample_guard.py`、`src/llm/output_contracts.py`、`src/llm/prompts.py`、`src/reporting/runner.py` | 新增提示词 `report.sample_guard` 与契约 `SampleDecision`（`action: aggregate\|narrow_range\|keep`，附 `reason`/`suggested_group_by`/`suggested_range`）：超限时让模型在「补 GROUP BY 聚合」与「缩小时间范围」之间选一个并说明理由，都不必要时选 `keep`。触发判据 `needs_sample_action`：`max(进入链路行数, 查询全量行数) > 上限` **或** 执行层已上报 `sample_capped`（执行层按同一上限先裁再回传，只比裁完的行数永远比不出超限）；外层快速闸门与内层决策**共用同一谓词**，不存在「外层放行、内层卡死」。**第一版只登记不自动重跑**：结论写入快照 `params.sample_decision` 与页面 quality 块的 `limitations`（经 `UnitResult.warnings`，且排在最前防止被分析模型的限制挤掉），重跑统一由补查回路处理，避免两条自动回路叠加；模型不可用、调用失败或「被上游裁过但真实行数未超限」时不调模型，直接给保守 `keep` 并如实写明两个行数（不互相冒充） | Phase 3 | 单测完成 | P1 |
| 23.30 | 缺同环比基期触发补查 | `src/reporting/profiling.py`、`src/graph/subgraphs/report_workbench.py` | 复现并修复事故：只拿回单月数据时产不出任何发现，而运行期补查回路的前置筛选要求「至少一条 high/moderate 发现」，回路因此永不触发、同比永远为 null。新增确定性判定 `needs_baseline_supplement`（单元问题命中「同比/去年同期/上年同期」或「环比/上月/上期」时，按样本数出聚合周期数与首尾跨度，不足即返回可读理由），接入 `report_decide_supplement` 作为与发现并列的**或关系**进入条件（额度/轮次/单元数三条硬前置对两者同等生效），全程不调用模型。门槛收敛为**单一真源**：判定侧与产出侧 `_period_findings` 共用 `_period_min_observations()` / `_has_enough_yoy_span()` 与同一处聚合算法（`_aggregate_periods` / `_period_span_days`），两侧都不许自带数字，杜绝「判定说足够、产出方产不出」的死区 | Phase 3 | 单测完成 | P1 |
| 23.31 | 单元标题由模型生成 | `src/llm/output_contracts.py`、`src/llm/prompts.py`、`src/reporting/models.py`、`src/reporting/planner.py`、`src/reporting/document.py` | `report.plan` 顺带产出 ≤12 字精简标题（与 question 同一次调用，不额外起模型调用），展示层 `unit_short_title(question, unit_id, title)` 优先用它，缺失时回退既有冒号切分/截断逻辑——发散单元与模型降级时标题必然为空，回退分支不是死代码。`planner._coerce_llm_units` 只做透传但按 `_UNIT_TITLE_MAX_CHARS`（20）收窄：契约 12 字上限拦的是模型，单元字段更宽，超长值会让整个单元通不过 `AnalysisUnit` 校验被静默跳过、业务问题一起消失。契约上限与展示层共用同一常量，避免「契约已放开、某处仍卡在旧值」的脱钩 | Phase 3 | 单测完成 | P2 |
| 23.32 | 提示词通用约束段共用 | `src/llm/prompts.py` | 把原先在对话侧与报表侧各写一份的三类重复措辞抽成模块级常量：`COMMON_EVIDENCE_ONLY`（名称与关系必须来自证据）、`COMMON_NO_CAUSAL_CLAIM`（无实验/对照/充分时间证据时只能描述相关性与共变）、`COMMON_NO_PADDING`（不得为凑数编造或选择不相关项），由对话侧（`sql.generate`、`analysis.result`）与报表侧（`report.plan`、`report.route`、`report.diverge`、`report.supplement`）**引用同一符号**而非复制文本，消除同一约束两处各写一份的漂移源。常量会被拼进模板本体并参与 `str.format` 渲染，**常量文本内不得出现花括号** | Phase 3 | 单测完成 | P2 |

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

### 2026-09-15 报表取数质量与可观测性批次（23.26～23.32）

对应 `docs/superpowers/plans/2026-09-14-report-data-quality-and-observability.md` 的 Task 1～4、6、7。Task 5（多图/多系列/可切换的图表契约）**本批次未实施**，已顺延并归 `spec/49-report-presentation-redesign.md` 主导。

- 时间范围（23.27）与快照重跑（23.26）先修，截断与样本处置（23.28/23.29）建立在前两者之上，缺基期补查（23.30）依赖时间范围与截断修复，标题（23.31）与提示词收敛（23.32）最后。
- 测试：新增 `tests/test_reporting/test_unit_snapshots.py`、`test_unit_time_range.py`、`test_sample_policy.py`、`test_supplement_for_missing_baseline.py`（28 例），扩展 `tests/test_reporting/test_report_content_shape.py`（模型标题与回退）与 `tests/test_llm/test_prompt_registry.py`（共享常量引用与变量契约）。子任务报告的验证记录：Task 1 相关套件 903 passed / 23 skipped；Task 2 相关套件 488 passed / 1 skipped；批次收尾 `tests/test_reporting tests/test_llm tests/test_graph/test_multi_source_truncation.py` 564 passed / 1 skipped；全量 `pytest -m "not live_llm"` 2961 passed / 3 failed，3 条均为**既有隔离问题**（`test_sql_rewriter_oracle_charset.py` 跨用例日志捕获顺序耦合、`test_unified_config.py` 比对正被并行会话修改的 `src/config.py`），与本批次无因果关系。**本批次的文档同步轮未复跑测试**。
- 迁移 `037_report_unit_snapshots.sql` 未执行到任何用户数据库，快照写入在本地与测试中走应用层模拟；真实 PostgreSQL 的 RLS 需另按 022 的修正模式验收。
- 未做真实模型端到端验收：`report.sample_guard` 的模型输出质量、缺基期补查在真实报表上的收敛情况、模型标题的可用性均待实机验证。

### 2026-09-14 第三批：报表内容构成（去明细、去噪音）

用户打开一份真实分享页后的反馈：「还有很多截断的数据」「不需要提供明细数据」「列表丑死了，可读性也差」。逐条查证后改动如下。

- **「截断」是误报，来源有三层**。① `runner` 曾把上游 `query_result_truncated` 直接透传成 `UnitResult.truncated`，而这个标记里混着"**给模型的样本被裁到 50/30 行**"（`multi_source.py` 的采样上限，有既有测试守着它的语义）——与用户可见行集无关。60 行的单元因此被判"样本被截断"，并被**逐条**写进 106 个发现条目，页面上满屏刷。已新增 `display_truncated(displayed, full_count)`：只看报表自己展示的行集是否少于全量，全量未知时不臆断。② 明细表格硬上限 100 行（19 张表里 6 张顶到），超出静默丢弃。③ 表格 `ellipsis: true` + 5~18 列挤在有限宽度，每格只剩几个字加省略号。后两层随明细表格一并移除。
- **明细表格块整体移除**：19 张「明细样本」表（最多 50 列 × 100 行）不再产出。明细由使用方回数据源查，页面只保留结论；图表下方的受控钻取（`drilldowns`，含列契约与行上限）保持可用，`table` 仍在块类型字面量里以兼容存量页面。
- **区段标题本地化**：22/24 个区段标题曾直接等于 `unit_id`（`sales_base_overview`、`three_way_reconciliation`），页面上整片英文。新增 `unit_short_title()`：问题形如「销售基盘：按年月…」时取冒号前的业务名，否则截断到 20 字。findings 块标题同样改用它（此前是 60+ 字的整段 question）。
- **findings 去统计噪音**：`_distribution_findings` 整体移除（P25/P50/P75/偏度是统计过程输出，不是可行动的业务发现），`_distribution_shape` 与 `_MIN_OBSERVATIONS["distribution"]` 一并清理；异常点的标签从「第 32 行」改为业务取值（时间列或维度列），定位不到标签就不产出——行号与 `z-score=3.25` 对用户没有意义。
- **relations 只留有效证据**：20 块里多数是"未发现共同字段"的 `no_relation`，占版面不提供信息。现只保留建立了关系的证据，全为无证据时该区段整体不出现。
- 验证：新增 `tests/test_reporting/test_report_content_shape.py` 8 例（去表格、标题本地化与回退、relations 过滤与整段消失、截断口径、分布/异常收敛）；`test_field_labels`、`test_multisource_evidence`、`test_runner_datasource`、`test_profiling_adapter` 四处按新契约更新并保留原意图。全量 `pytest -m "not live_llm"` 2876 通过。**存量报表页面不会自动变化，需重新生成**。

### 2026-09-14 第二批：状态语义、计划卡死、展示与筛选

用户在一次实机走查里提出的七项优化，均已落地。

- **大模型规划链路其实一直没生效（根因是输出契约写死 12）**：日志 `报表规划模型不可用，回退规则拆分  error_type=ValidationError` + `List should have at most 12 items after validation, not 16`。模型**正确拆出了用户要求的 16 个单元**，却被 `ReportPlanUnitsOutput.analysis_units` 的 `max_length=12` 拒收，异常降级成规则拆分——所以才出现「经营总览】」这类标题残渣和 `sales`/`unit_1` 这种英文标识。与上一批的 `query_count le=64` 是同一个坑换字段。已改为契约只留理智上限（`_MAX_PLAN_UNITS_SANITY=200`，测试守住 `>= _UNIT_COUNT_STATIC_MAX`），业务上限由 `max_plan_units()` 裁剪；`report.plan` 模板的单元数改为 `{max_units}` 经 `system_values` 从配置下发，并补上"question 必须自包含、不得只写标题"（直接针对标题残渣）。**真实模型端到端验证**：事故 prompt 重跑 → 16 个单元、无降级 warning、unit_id 为 `sales_baseline`/`three_way_reconciliation`、question 全部是完整中文业务问题。
- **「计划生成中」卡死无路可走**：后台规划任务活在进程内，服务重启后记录停在 `planning`——重试接口只认 `plan_failed`、删除接口拒绝 `planning`，用户被锁死。新增 `PLAN_STUCK_SECONDS=300` 与 `plan_is_stuck()`：`planning` 且超过阈值即视为卡死，允许中断重来；阈值同时经 `_public_report` 的 `plan_stuck` 下发给前端（前端不自算，避免两处阈值漂移），列表页据此显示「中断并重新生成」。刚入队仍在正常生成的会被 409 拦住，不会误伤在途任务。
- **状态文案有歧义**：`planning` 由「生成中」改为「**计划生成中**」，`draft` 由「未生成」改为「**报表未生成**」——两个状态都同时牵扯"计划"和"报表"，原文案看不出在说哪一件。i18n 词条同步。
- **单元列表在报表详情页仍是只读（上一轮"改了没生效"的真相）**：预览页与详情页**各写了一份**单元列表渲染，上一轮只改了预览页。已抽出共享组件 `ReportPlanUnitRow`（勾选框全站只此一份），两个页面的 user/diverged 两组都改用它；新增测试同时盯住"页面不得自己实现勾选逻辑"。顺带把 `unit_id` 从列表正文移除——它是英文内部标识，当标题展示会让人以为"生成的标题全错了语种"，展示一律以 `question` 为主。
- **通知点击白屏**：通知 link 写的是 `/report-workbench/reports/{id}`，而路由表里只有 `/report-workbench/jobs`，`navigate()` 匹配为空 → 内容区一片空白。三处修复：① link 改为真实路由 `/report-workbench/jobs?report={id}`；② 路由表加 `path="*"` 兜底重定向，任何失效链接都不再白屏；③ 列表页支持 `?report=` 深链直达该报表详情。新增交叉契约测试：**从 `App.tsx` 提取真实路由表，校验后端写的每个站内 link 都被某条具体路由覆盖**（通配兜底不算命中，否则校验形同虚设）。
- **标题悬停塞满长文**：列表 tooltip 直接渲染 `item.prompt` 全文（可达数千字）。改为 `promptSummary()` 截取 100 字，完整需求原文移到详情抽屉的「计划需求」区展示；单元格标题继续优先用大模型生成的标题。
- **用户筛选要背 ID**：报表信息的用户筛选从裸 `InputNumber` 改为可搜索下拉，`GET /admin/users` 新增 `search` 参数按用户名 `ILIKE` 模糊匹配，前端 `fetchReportUsers` 边输边搜（服务端过滤，不在已加载的前 50 条里筛），已选项在换词后仍保留标签。
- **报表列表返回用户名**：列表与详情新增 `username`。PG 分支 `LEFT JOIN users u ON u.id=j.user_id` 取登录名；内存分支由 `RequestIdentity` 新增的 `username` 字段（来自 `get_current_username()`）随行写入——只改一边会让两种部署方式表现不一致，测试同时盯住 SQL 与内存分支。

### 2026-09-14 计划失败与渠道配置修复批次

一条真实报错（`plan_failed`）牵出三类缺陷，均已修复并补上能复现原报错的回归。

- **`query_count` 上限再次写死击穿（本次 `plan_failed` 的直接原因）**：字段上界在上一批次从 16 提到 64，注释写的是"18 单元 × 平均 4 源"。但 `report_max_units_per_run` 已由超管放开到 36、用户选满 5 个数据源、deep 档还带 6 个发散额度，于是 `(22 + 6) × 5 = 140` 直接击穿 64，`planner.parse()` 在构造 `EstimatedResources` 处抛 ValidationError 并冒泡成 `plan_failed`。**实测复现**：把事故 prompt 原样喂给 `ReportPlanner._build_units` 得 22 个单元、`max_plan_units()=36`，复算正好 140，与日志 `input_value=140` 吻合。已改为与既有单元数模式对齐：静态上界 `_QUERY_COUNT_STATIC_MAX = 1000`（等于 `report_max_queries_per_run` 可配的最大值）只做契约兜底，真实钳位走新增的 `max_plan_queries()` 读配置，**planner 与 `runner._reserve_plan` 共用同一函数**（此前两处各自写死 `min(1000, ...)`，与字段契约脱钩；`model_copy` 不校验，运行期那处会把超限值静默写进去）。修订记入 `spec/42` §11.4。
- **22 个单元里有 6 个是标题残渣**：`_SECTION_MARK_RE` 把章节号与条目号一视同仁，`【一、经营总览】` 这类分组标题各成一个单元（`经营总览】`、`财务对账】`…）。根因是**提示词与正则的同一个概念缺陷**：`report.plan` 模板原文写"用户显式写出的章节（如 1./2./一、）一个章节只拆一个单元"，把两类编号并列成同一层，两层编号的需求模型无从判断该按哪层拆。按用户要求**不做符号映射**（写死"某个符号是章节、某个符号是单元"换种编号形式又会失效），改为按嵌套层级判断的通用原则：某编号下还嵌着更小编号时，外层只是分组标题，**最内层编号才对应一个单元**；只有一层编号时每个编号就是一个单元。
- **预览页「你要求的分析」是只读的**：该分组（`origin=user`）只渲染 `List.Item`，没有勾选框，只有 diverged 分组能取消。于是上面那 6 个标题残渣**用户删不掉**，只能整份重新生成。而后端契约本就支持删减任意单元（`POST /jobs` 与 `PATCH /reports/{id}` 的 `accepted_unit_ids` 都是"只允许删减"，不区分来源），属前端实现与契约脱节。已改为两个分组**共用同一个带勾选框的 `renderPlanUnit` 渲染器**（各写一份必然再次出现"有一组只读"），并补空选集保护：全部取消时禁用确认按钮并提示"至少保留一个分析单元才能生成报表"，避免撞服务端 422。
- **通知渠道配置读取失败（同一次请求里的第二个报错）**：`get_channels` 用 `dict(row["config_json"] or {})` 解析，而**本环境的 asyncpg 把 jsonb 按文本解码**（`SELECT '{}'::jsonb` 实测返回 `str`），`dict('{}')` 逐字符迭代抛 `ValueError: dictionary update sequence element #0 has length 1`，整条渠道读取失败 → 邮件/飞书/Slack 全部退化成仅站内信。**排查警告**：第一版按"写入侧双重编码"改了代码，被实测推翻 —— 库里数据是好的（4 行全是 033 播种的 `{}`，`jsonb_typeof` 实测 `object`，`updated_at` 自播种未变），而 asyncpg 对该环境的 jsonb **参数只接受字符串**（传 dict 直接 `DataError: expected str, got dict`），所以原来的 `json.dumps(...)` 写入是正确的、已改回。真正缺的是读取侧的 `json.loads`。代码库其余 jsonb 读取点（`runtime/settings.py`、`knowledge/schema_sync.py`）早已写成 `json.loads(x) if isinstance(x, str) else dict(x)`，**只有这里漏了**；现已抽成 `_as_config_dict()` 对齐同一约定。
- 验证：新增 `tests/test_reporting/test_planner_query_budget.py`（4 例，含 (22+6)×5=140 的越界回归与 runner 共用上限）、`tests/test_reporting/test_plan_unit_layering.py`（2 例，断言提示词按嵌套层级表述且旧并列举例已消失）、`tests/test_frontend/test_report_plan_editing.py`（3 例，共用渲染器 + 空选集保护）、`tests/test_notifications/test_channel_config_codec.py`（3 例，写侧序列化 + 读侧文本解析）。**真实库验证**：`get_channels(3)` 修复前抛 `ValueError`、修复后返回 4 个渠道；写读往返用临时表验证（服务端 `jsonb_typeof` = `object`）。全量 `pytest -m "not live_llm"` 2845 通过；`tsc --noEmit` 通过。另有 2 个**既有且与本批次无关**的失败：`tests/test_tools/test_sql_rewriter_oracle_charset.py` 的两例只在全量运行时失败、单文件运行 49 例全绿（跨用例状态污染），且该文件与其被测模块对 HEAD 均无改动、不依赖本批次改动的任何文件，属既有隔离问题，未在本批次处理。

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

## 2026-09-13 深度报表真实验收续修

- 对应既有 23.1、23.3、23.5、23.10、23.16：本轮修复已完成单测、编译图边界回归及下述真实生成路径验收；模块仍为 22 项已完成、0 项待开发，不新增功能编号。
- 年度标题中的 2026 年传入全部默认时间范围，发散单元继承 2026-01-01 至 2026-12-31；去重键加入业务问题与路由来源，不再把退款、库存等不同问题复用成销售结果。
- `suppress_supplemental_query` 必须声明为真实 `AnalysisState` 通道，仅在 runner 注入而不声明会被编译图丢弃。报表不再触发旧的无档位补查回环。
- ClickHouse 共享只读客户端关闭自动 session ID，避免深度并发查询相互冲突；DataSourceRegistry 注册异步关闭器，等待缓存连接及失效释放任务，防止事件循环关闭后连接池析构报错。
- 多源统计、UnitResult 和页面使用有序联合列集合，异构来源的缺失列不补零。模型代表行按来源交错，单独提供确定性样本日期边界及各来源 SQL/计数；最终分析不再被仅看前三行的旧摘要覆盖。列数和数值类型相同不能证明指标相同，只允许已知同义别名及可核验 SQL 表达式别名归一。
- 模型返回多个 JSON 对象时不拼接或丢弃后面的限制，使用同一总超时预算最多重新生成一次；连续不合法仍保留异常堆栈并回退。销售问题词面检索补回订单事实表；CTE 缺列漏检详见 26.1 回归记录。
- 真实验收使用 test2 身份，正式认证、预览/创建/重跑 API、真实租户模型和五种数据源，不使用 Mock。第六轮任务曾为 partial；针对原任务重跑，第八轮发布 `45b1b8fb-fd98-4a19-bb85-233abe723884` 第 3 版，run 为 `294613dd-0107-4965-bde5-9f5f51bbc566`，12 单元、deep、立即一次、7 天，2026-09-20 22:25:00（Asia/Shanghai）到期。
- 最后一次运行 17 次 SQL 全部成功、36 次 LLM 传输调用无失败，日志无 ERROR、超时、异常关闭；仍保留 CTE 判定盲区、只读账号风险提示和被校验器拦截后修正的 WARNING，不以删除日志冒充无问题。日志为 `logs/report-acceptance-round8.log`，真实产物为 `logs/report-acceptance-final-45b1b8fb-fd98-4a19-bb85-233abe723884.json`。
- 浏览器实际登录并读取第 3 版，刷新后 `/auth/me` 和报表 GET 均 200，无 pageerror；所有表格联合字段均可见，无旧“当前请求未实际执行任何查询”或默认 90 天文案。桌面/手机截图和摘要在 `logs/report-acceptance-v3-*.png`、`logs/report-acceptance-browser-v3.json`。
- 离线扩大回归：reporting、graph、security、datasource、knowledge、llm、tools 共 1566 通过、1 跳过、1 排除，保留 1 条既有统计警告；排除 live_llm/local_llm/live_datasource/browser，单测未调用真实模型。日志为 `logs/report-final-regression.log`。
- 报表 API 补充回归 15 通过，日志为 `logs/report-final-api-regression.log`。本轮其他修改文件 Ruff 通过；`src/knowledge/schema_manager.py:877` 存在 HEAD 中已有的局部导入排序问题，本次只修改销售词面映射，未夹带格式化无关方法。
- 验收边界：运行成功不等于业务指标全量完备。销售样本受行数上限限制，同比基期、成本中心明细与跨源地区/指标口径仍存在缺口；报告必须披露，不能将触点数量冒充金额、代理指标冒充气温或将相关升级为因果。页面标题仍采用既有默认“经营分析报表”，不是用户自定义书名号标题。
- 部署边界：8000 是未启用热更新的原 PyCharm 进程，停止/另启后台服务被环境执行策略拒绝。本轮在前台运行真实 FastAPI lifespan/ASGI API 完成生成并持久化，原服务可读取新版本；未声称原服务已加载新代码。后续从旧进程生成新任务前仍需用户重启后端。历史版本和中间验收报表保留，不删除审计。

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

## 2026-09-15 取数质量批次最终评审修复（23.28～23.32 收尾）

- 对应既有 23.26、23.28、23.29、23.30、23.32，不新增功能编号；三项 Important 与一项 Minor 全部修复，细节见 `.superpowers/sdd/final-fix-report.md`。
- 23.26 快照：截断原因里的上限从「页面渲染口径 200」改为真实切割点 `report_max_sample_rows`（`runner._sample_row_limit()` 为唯一读取处，`DISPLAY_ROW_LIMIT` 已删除），文案统一为「进入分析链路 X 行，查询全量 Z 行（生效样本上限 Y 行）」，与 `sample_guard` 的计数口径一致。
- 23.30 补查：新增 state 键 `supplement_context`（触发来源 + 理由）；理由进补查提示词新增的 `baseline_reason` 分区、进触发单元的 `warnings`（页面 quality 块）。由缺基期触发时不再要求 `supports_finding` 非空，由显著发现触发时保持必填；`report.supplement` 模板升版 1.1.0。
- 23.30 判定侧与产出侧输入派生同源：新增 `profiling.profile_inputs()`（列取 `UnitResult.columns`、指标取 `resolved_units` 已过授权过滤的那份），`needs_baseline_supplement` 的 `columns`/`metrics` 改为必填关键字参数，消除「判足够但产出方产不出」的单向死区。
- 23.32 守护断言补源级检查：新增 `COMMON_*` 引用处计数 + 「常量文本只允许出现在定义处字面量」两条断言，堵住「把常量抄回模板仍为绿」的盲区（两种变异均实测变红）。
- 验证：全量 `pytest -m "not live_llm"` = **2 failed（既有 `test_sql_rewriter_oracle_charset` 隔离失败）/ 2969 passed / 24 skipped**，本任务新增 4 条用例；未提交。
