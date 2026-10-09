# DataAnalysisAgent — 代码导航指南

## 项目概述

LLM 驱动的数据分析智能体。用自然语言提问，自动完成：轮次状态初始化 → 意图识别 → 表结构检索 → SQL 生成 → 本地安全校验 → 目标库 EXPLAIN → 执行 → 分析 → 图表生成 → 响应组装。

**技术栈**：Python 3.14.0 + FastAPI + LangGraph + LangChain + sqlglot + SQLAlchemy + React + TypeScript。

## 目录结构

```
├── src/
│   ├── api/              ① Web 接口层 — 访问策略/领域路由/纯 ASGI 认证/安全头 + SSE 流式；含 Grafana SimpleJSON 与优化建议
│   │   └── public_stream.py 公开 JSON 正文/章节增量提取，不输出内部推理
│   ├── graph/            ② 核心流水线 — LangGraph 主图 + 可复用 SQL 子图
│   │   ├── attribution.py   触点归因模型选择、路径数据资格和补查数据集编排
│   │   ├── context.py       请求/权限/路由/执行四组轻量上下文适配
│   │   ├── contracts.py     任务计划与多数据源请求/结果 Pydantic 契约
│   │   └── nodes/           状态准备、SQL 主链、直接回答与多源节点
│   │   └── subgraphs/       SQL 复用、结果展示及文件/研究/预测/报告/动作能力子图
│   ├── llm/              ③ LLM 调用层 — Provider 注册表 + 租户连接选择 + 适配器 + 版本化 Prompt
│   │   └── adapters/         模型适配器
│   ├── datasource/       ④ 数据源管理 — 注册/发现/Schema/凭证加密
│   │   └── providers/        数据源提供者
│   ├── connectors/       ⑤ 数据库连接器 — 自注册方言与统一运行时策略
│   │   ├── plugins.py       可信 entry point 热发现、目录元数据与新执行撤权
│   │   ├── seed_writer.py   显式测试库预检、分批事务写入、增量扩容与拒绝覆盖保护
│   │   └── seed_verifier.py 只读核验 625 张表的计数、指标、关联键和中文样本
│   ├── testdata/            五源各 125 表中文造数、五方言 DDL、强弱关联与分级知识资料
│   │   └── expanded_catalog.py 50 个业务主题的 500 张新增专属表，旧表使用独立计数池保持兼容
│   ├── knowledge/        ⑥ 知识库 — 三范围治理 + 标签 + 连接级缓存 + 文档摄取
│   │   └── schema_sync.py   跨 Worker 同步任务、取消清理和规范结构快照
│   ├── runtime/            平台热配置 — 启动默认值、数据库覆盖、版本审计与跨 Worker 刷新
│   ├── memory/           ⑦ 记忆系统 — 会话持久化 + 上下文裁剪 + 历史 + 成功 SQL 自进化
│   ├── automation/       ⑧ 主动洞察与定时报告 — PG 调度 + 重授权执行 + 通知分发
│   ├── tools/            ⑨ 分析工具 — 统计、预测与跨资产分析
│   ├── optimization/     ⑨.1 确定性 SQL 优化建议核心，不执行 DDL
│   ├── market/           ⑩ 行情 Provider 与 PostgreSQL 持久化（当前 Tushare/A 股）
│   ├── actions/          ⑪ 受控外部动作（人工确认、幂等、审计）
│   ├── reporting/        ⑧.1 报表工作台 — 自然语言计划、任务运行、关系证据、动态 DSL、分享与配额
│   ├── security/         ⑫ 安全模块 — API/IP 策略 + SQL 统一执行 + Schema 校验 + 脱敏 + 限流 + 审计 + 出站策略
│   ├── db/               ⑬ 状态库基础设施 — 版本化迁移 + URL 工具
│   ├── mcp_client/       ⑭ MCP 集成 — 客户端管理 + 工具暴露
│   ├── mcp/              ⑭.1 MCP Server 稳定模块入口（python -m src.mcp）
│   ├── app_context.py        应用级依赖容器 + ASGI 请求绑定 + 资源关闭
│   ├── config.py             配置管理 (pydantic-settings)
│   ├── bootstrap.py          分阶段启动/关闭编排
│   ├── tenant_purge.py       租户关系型、向量和运行时资源的可重试物理清理
│   ├── skill_manager.py      技能发现、作用域与激活
│   ├── skill_security.py     Skill 包摘要与 Ed25519 验签
│   ├── skill_runtime.py      Skill 契约、资源和隔离执行入口
│   └── skill_worker.py       非内置 Skill 子进程 Worker
├── frontend/                 React SPA (Vite + Ant Design + ECharts)
│   ├── src/hooks/chatPresentation.ts 公开流 reducer、并行隔离与过程摘要状态
│   ├── src/i18n/                中文默认、英文词典与可订阅语言状态
│   ├── Dockerfile               Node builder + Nginx runtime
│   └── nginx.conf               SPA、API 代理和 SSE 配置
├── skills/                   三十三个 system 内置 Skills（质量/报告 + 底座、治理与 SQL 优化）
├── data/skills/              tenant/private 受管 Skills
├── spec/                     技术规格（15 个章节）
├── features/                 功能清单（19 个模块）
├── tests/                    测试
├── migrations/               按编号执行的 PostgreSQL SQL 迁移
├── docs/metrics/             业务指标文档
├── Dockerfile                Python builder/runtime 后端镜像
├── docker-compose.example.yml Linux 生产编排模板（实际 compose 文件忽略）
└── .dockerignore             后端构建上下文过滤
```

## 核心数据流

```
POST /api/v1/chat {"query": "本月 GMV 排名？", "stream": true}
  │
  ├─ API 访问策略        YAML/PG 策略 → 可信代理 IP → deny/allow → 分级访问日志
  ├─ API 安全入口        public/optional/JWT/Admin Key/超级管理员认证 → 请求预算 → 数据源授权候选
  ├─ prepare_turn        保留轻量历史/快照，清理瞬态字段并重建四组请求级上下文
  ├─ classify_intent     意图 + TaskPlan 能力 + Skill 候选集
  │    ├─ 文件分析 / 市场研究 / 外部动作 → 对应独立子图
  ├─ restore_previous_result  仅 meta 且数据源一致时从 HistoryStore 恢复上轮富结果
  ├─ retrieve_schema     SchemaManager 三级回退 → 表结构 + 知识上下文 + Skill 精确激活
  ├─ generate_sql        LLM 生成 SQL（对话历史注入 + 重试上下文 + Pydantic 契约）
  ├─ layer3_validate     委托统一 SQL 服务做 AST 只读安全校验 + 列引用归属校验（两阶段取数：先 relevant_tables 零 IO，出现 high 置信问题或阶段一无基准时才用权威全量快照复核）
  ├─ layer4_explain      统一 SQL 服务按 Registry 权威方言执行 EXPLAIN
  ├─ execute_sql         统一有界执行（权限 / 限流 / 脱敏 / 审计）
  ├─ analyze_result      确定性统计 + 业务关联/影响/原因/建议 + 证据充分性判断
  │    ├─ 触点归因 → 用户显式模型/受约束模型推荐 → 规则资格复核 → 五种确定性算法
  │    ├─ 路径证据不足且 Schema 可补齐 → supplemental_query → 固定原模型重新计算
  │    ├─ 证据可由当前 Schema 补齐 → supplemental_query → analyze_result（最多两轮）
  │    ├─ 预测 → forecast_subgraph（回测/区间/模型卡）
  │    └─ 报告 → report_subgraph（custom-report 真实执行）
  ├─ generate_chart      默认 SQL 分析的 ECharts 配置生成
  └─ build_response      响应组装 + 轻量会话历史 + HistoryStore 富结果 + 成功 SQL 自进化观察
```

## 各模块说明

### ① `src/api/` — Web 接口层

| 文件 | 职责 |
|------|------|
| `routes/__init__.py` | 组合各领域 APIRouter，并保留旧模块导出兼容 |
| `routes/*.py` | chat、datasource、schema、session、mcp、knowledge、skills、management、admin、llm_admin、access_policy、Grafana、optimization 领域端点 |
| `routes/admin.py` | 平台租户/用户治理；租户删除以 PostgreSQL advisory lock 跨 Worker 互斥，以两阶段 RLS 事务委托 `tenant_purge.py` 清理全部作用域资源 |
| `routes/automation.py` | 自动化任务 CRUD、暂停/恢复、立即运行、身份隔离运行记录和用户私有站内通知 |
| `routes/grafana.py` | SimpleJSON search/query/annotations，ACL 快照、有界并发、超时和多目标故障隔离 |
| `routes/optimization.py` | 确定性 SQL 优化建议 API，自动获取 Schema/索引/统计证据，只读分析不执行 DDL |
| `schemas.py` | Pydantic 请求/响应模型 |
| `streaming.py` | SSE 流式：脱敏 data/chart 先到，公开正文及章节增量按 node/stream_id 隔离；管理员开关控制业务过程摘要 |
| `public_stream.py` | 不完整 JSON 的顶层公开字段白名单提取、转义解码，内部 SQL/工具正文/原始推理禁止进入正文 |
| `middleware.py` | 异常 → HTTP 状态码映射 |
| `auth.py` | 全模式强制 JWT/Cookie 认证、登录限流与原子账号锁定，身份 ContextVar 覆盖完整流式响应生命周期 |
| `access_policy.py` | 纯 ASGI 接口 IP 阻断、策略下传和 standard/security/audit/none 聚合访问日志 |
| `background_tasks.py` | API 后台任务强引用、完成回调和异常记录统一入口 |
| `security_headers.py` | CSP/HSTS/防嵌入/nosniff 纯 ASGI 响应头 |

### ② `src/graph/` — 核心流水线

| 文件 | 职责 |
|------|------|
| `state.py` | `AnalysisState` TypedDict（会话持久字段 + `UntrackedValue` 请求字段和四组轻量上下文） |
| `context.py` | 请求、权限、路由、执行上下文 Pydantic 模型与扁平字段兼容适配 |
| `contracts.py` | `TaskPlan`、`SourceQueryRequest`、`SourceQueryResult`、`MultiSourceResult` 契约 |
| `indicator_catalog.py` | 指标目录问法识别、非指标字段过滤、白名单内 LLM 复检、主题化展示与确定性 DISTINCT SQL |
| `attribution.py` | 用户显式归因模型优先、未指定时受约束推荐；固定选择跨补查轮次复用，并确定路径证据是否可补齐 |
| `workflow.py` | 从节点目录装配 StateGraph，显式保留条件业务路由 + Checkpointer |
| `node_registry.py` | 节点 handler 与流式进度文案目录 |
| `nodes/prepare_turn.py` | 固化上一轮轻量索引，压缩旧历史并清空当前轮 SQL/错误/结果/分析状态 |
| `nodes/restore_previous_result.py` | 校验数据源集合后从 HistoryStore 恢复上轮 SQL、结果样本和统计 |
| `nodes/classify_intent.py` | 意图分类 + Skill 激活 |
| `skill_activation.py` | 意图候选集、Schema 精确激活、复合资源 ID 和预算聚合 |
| `subgraphs/sql_analysis.py` | 主图与多源 worker 共享的 Schema、分解、SQL、校验、EXPLAIN、执行拓扑装配 |
| `subgraphs/result_presentation.py` | 多源合并复用的分析、图表固定边与精确分析短路子图 |
| `subgraphs/file_analysis.py` | 授权文件/MCP 工具执行与统一失败语义 |
| `subgraphs/market_research.py` | 授权外部证据研究简报，保留证据范围限制 |
| `subgraphs/forecast.py` | 时间序列预测、滚动回测、区间和模型卡 |
| `subgraphs/report.py` | custom-report 工具执行和 report Artifact 数据准备 |
| `subgraphs/action.py` | 人工确认、幂等和默认拒绝的外部动作分发 |
| `nodes/retrieve_schema.py` | Schema 检索 + 知识库上下文 + 按真实表名再次激活 Skill |
| `nodes/generate_sql.py` | LLM SQL 生成（独立表名白名单、分类重试、结构化补参）与行式指标目录确定性 SQL；重试回喂按错误类型分流，schema 幻觉分支不回注失败 SQL 只回喂可用列清单 |
| `nodes/layer3_validate.py` | sqlglot AST 只读白名单与危险语句阻断；列引用归属校验前移到 EXPLAIN 之前，判定委托 `security/sql_schema_validation.py`；两阶段取数（阶段一 `relevant_tables` 零 IO，出现 high 置信问题或阶段一无基准时才用 `get_or_fetch_schema(ds, user_query="")` 全量快照复核，复核不可用回落阶段一结果），幻觉列在此处失败并把可用列清单喂回重试 |
| `nodes/execute_sql.py` | SQL 执行（空 SQL 跳过保护）；列引用校验与列归属探针作为最后一道防线，已收敛为委托 `security/sql_schema_validation.py` 的兼容适配器 |
| `nodes/multi_source.py` | 契约化多源 worker、并行执行、维度/指标列对齐和来源失败隔离 |
| `nodes/analyze_result.py` | 确定性统计处理器 + 证据化业务关联、影响、原因假设和行动建议分析 |
| `nodes/supplemental_query.py` | 证据不足时在当前权限内复用完整 SQL 安全链补查，并把脱敏证据回注分析节点 |
| `nodes/generate_chart.py` | 按行数与列语义确定图表资格，输出成功、不适用、失败或核心数值契约 |
| `nodes/build_response.py` | 响应组装、最终 SQL 列表 + 轻量 checkpoint 历史 + 富结果持久化 |

### ③ `src/llm/` — LLM 调用层

`knowledge_loop.py`（统一有界知识检索工具循环）+ `client.py`（兼容工厂 + local/remote/none 任务路由）+ `provider_registry.py`（协议别名与 Provider 注册）+ `tenant_config.py`（租户命名连接、默认值、凭证解密、模型能力和请求级推理 ContextVar）+ `capability_schema.py`（厂商动态能力表单和值校验）+ `invocation.py`（统一调用出口）+ `adapters/` + `prompts.py` + `prompt_budget.py` + `output_contracts.py`。`routes/llm_admin.py` 由 super_admin 维护、启停和物理删除厂商/模型目录，由 tenant_admin 维护当前租户连接和默认值；Chat/SSE 入口解析 `llm_connection_id + model_id + reasoning_enabled + reasoning_effort` 后绑定请求上下文，未配置租户连接时失败关闭，不再回退全局凭证。DeepSeek V4 Pro/Flash Adapter 统一映射 thinking、high/max 和 reasoning_content 工具链回传，原始推理不进入公开响应或历史。Prompt 通过稳定 ID/多版本注册，支持激活和回滚；System 与动态上下文共享字符预算，所有业务调用统一附加 Prompt/任务追踪 metadata，节点输出优先由 Pydantic 校验；远程模型凭证和模型选择统一来自租户管理后台，轻量节点优先 `LOCAL_LLM_*`。

### ④ `src/datasource/` — 数据源管理

`transfer.py` 定义带版本的 JSON 迁移契约与凭证过滤；数据源管理页提供导入/导出及逐源同步状态。Provider 的 `import_configs()` 使用单事务新增配置与权限，提交后发布内存状态，重名时按 6 位 base36 随机后缀改名并在响应回传映射，不做静默覆盖。启动只恢复 `datasource_configs`，不再从 YAML/环境变量/ORM 自动发现或注册演示库；`setup.py` 仅保留测试辅助用途。表结构页面只读，认证等待和普通 API 均有有界超时，详见 `spec/37-runtime-platform-and-ui.md`。

`registry.py`（全局 Provider、Oracle/ClickHouse 方言适配、`resolve_or_none` 不抛异常；ClickHouse 按 `extra_params.connect_timeout` 做 TCP 建连探针）+ `credential_manager.py`（无源码默认主密钥、每次随机 salt 的 `v2:salt:token` Fernet 密文、历史 token 兼容 + 环境变量凭证）+ `setup.py`（SQLite 演示库）。

### ⑤ `src/connectors/` — 数据库连接器

6 种方言通过 `connectors/registry.py` 自注册：ClickHouse / MySQL / PostgreSQL / SQLite / Oracle / MSSQL。超时、EXPLAIN、探针和 Engine 参数由各 Connector 封装；ClickHouse 在网络调用前校验全部解析地址并固定使用已验证 IP；非 SQLite 连接 URL 通过 SQLAlchemy `URL.create()` 保存，字符串展示默认隐藏密码。

`plugins.py` 把内置类型和 `data_analysis_agent.connectors` entry point 扩展放入同一目录，数据源表单从 `/datasource-types` 取得类型及连接字段，指标方言从服务端数据源带出。平台发现/启停状态持久化到运行时配置表；缓存连接器的执行入口每次检查撤权。新增驱动先通过部署侧安装到所有 Worker，再在后台发现；不允许 HTTP 上传代码或在线 pip。改换已加载模块/原生驱动版本仍应滚动部署。

### ⑤.1 `src/runtime/` — 平台动态配置

`settings.py` 以 `LIVE_FIELDS` 显式限制可热改范围，环境/YAML 为启动默认值，PostgreSQL 覆盖优先。后台支持完整类型/范围校验、乐观锁冲突、恢复默认与审计；各 Worker 每两秒刷新，故障保留最后有效版本。主数据库、密钥、认证模式、存储后端等启动依赖不进入热改白名单。接口与部署边界见 `spec/37-runtime-platform-and-ui.md`。

### ⑥ `src/knowledge/` — 知识库

| 文件 | 职责 |
|------|------|
| `datasource_cache.py` | 按连接指纹共享数据库元数据，支持本地 JSON / Redis 配置切换 |
| `schema_manager.py` | 连接级精确缓存 + VectorStore 语义索引 + 文档/DB 内省回退；精确命中不重复向量化 |
| `schema_sync.py` | PostgreSQL 会话锁互斥、持久化任务状态、取消清理、规范快照发布和跨 Worker 读取 |
| `asset_models.py` | `DataAsset` / `Evidence` / `AnalysisPlan` / `AnalysisArtifact` 统一数据契约 |
| `retrieval.py` | 知识租户/可见性/数据源过滤、VectorStore 检索和 Citation 转换 |
| `content_safety.py` | 外部文档注入检测、证据分隔渲染和工具指令隔离 |
| `reranker.py` | 向量/关键词/短语分数融合、来源多样性惩罚和确定性重排 |
| `retrieval_eval.py` | Recall@K、MRR、引用命中率和租户越权召回评测 |
| `structured_assets.py` | CSV/Excel/Parquet 统一读取、列级质量 profile、时间列/候选主键识别 |
| `structured_query.py` | DuckDB 临时表注册、SQLGlot 只读校验、结果行数限制和多 Sheet 表映射 |
| `document_assets.py` | 结构保真 PDF/Word/Markdown/HTML 解析，输出页码、段落、标题和表格 Citation 定位 |
| `doc_parser.py` | PDF/Word/TXT/MD 文本提取 + 4 种分块策略（AUTO/HEADING/PARAGRAPH/FIXED） |
| `governance.py` | system/tenant/private 三范围授权，区分 super_admin 与 tenant_admin |
| `tag_store.py` | PostgreSQL 全局/个人标签搜索、创建、停用与提升治理 |
| `file_store.py` | 原始文档 PostgreSQL 存储、三范围 ACL 与 RLS 身份上下文 |
| `system_scanner.py` | SYSTEM_KNOWLEDGE_DIRS 递归扫描、checksum 幂等系统知识摄取 |
| `upload_manager.py` | 有界异步上传任务、终态 TTL 回收、范围/标签 metadata 和后台 VectorStore 写入 |
| `business_rules.py` | 业务规则检索 |
| `doc_loader.py` | Markdown 文档加载与索引 |

知识检索固定拆成 `system`、当前租户 `tenant`、当前用户 `private` 三组过滤，分别召回后去重重排。文档上传先执行角色授权，再把范围、数据源和标签同时写入原文件表与 VectorStore；系统目录扫描固定写入 `system` 范围并按 checksum 跳过重复内容。

### ⑦ `src/memory/` — 记忆系统

| 文件 | 职责 |
|------|------|
| `checkpointer.py` | `AsyncPostgresSaver` + `MemorySaver` 工厂（自动创建 PG 库；Windows 使用 SelectorEventLoop） |
| `context_builder.py` | 上下文裁剪（热/温/冷三层） |
| `history_store.py` | PostgreSQL 查询历史 + 内存环形缓冲回退；工作流 await 写入，final_result JSONB 持久化逐轮 SQL、数据、分析和图表，不保存原始推理 |
| `long_term_store.py` | 长期记忆（ChromaDB + PG 双写） |
| `knowledge_evolution.py` | 成功 SQL 观察聚合、数据库 Claim Token、多 Worker 超时接管和稳定 ID 幂等晋升 |
| `session_archive.py` | 会话归档 |

### ⑧ `src/automation/` — 主动洞察与定时报告

`AutomationStore` 使用 PostgreSQL `SKIP LOCKED` 认领到期任务，并提供身份隔离的暂停/恢复和最近运行记录；`ScheduledAnalysisRunner` 每次运行重新检查数据源、行列权限和只读 SQL；`NotificationDispatcher` 的 SMTP/飞书/Slack 只读取服务端配置，站内通知持久化到 PostgreSQL。前端工作台明确区分主动洞察与定时报告，展示调度状态、运行结果和失败摘要。

### ⑨~⑫、⑭ — 工具/行情/动作/安全/MCP

| 模块 | 职责 |
|------|------|
| `tools/knowledge_search.py` | 只读知识检索工具：`KnowledgeSearchArgs`（extra=forbid）、身份复核、数据源白名单+授权、未绑定下推、状态化返回（ok/empty/invalid_arguments/forbidden/timeout/unavailable/budget_exhausted）与受限引用 |
| `tools/analyzer.py` | 描述性统计/趋势/Z-score 异常/集中度/Pearson 相关 |
| `tools/chart_presentation.py` | 受约束 ChartPlan、语言与字段标签、六类展示共用构建器、类型切换角色适配；日期主轴、额外维度分组/分面、重复坐标显式聚合、缺失 null 连线 |
| `tools/touchpoint_attribution.py` | 路径字段映射与资格复核；首次、末次、线性、时间衰减和 U 型归因的 Decimal 守恒计算 |
| `tools/forecasting.py` | naive/线性预测、rolling backtest、预测区间和模型卡 |
| `tools/forecast_engine.py` | ForecastRequest、可注册 ForecastModel、滚动回测和统一 ForecastResult |
| `tools/market_analysis.py` | MarketDataProvider 契约和行情收益/波动/回撤指标 |
| `tools/scenario_planning.py` | 受约束情景组合、资源上限和方案评分排序 |
| `tools/join_contract.py` | 跨资产 Join 的匹配率、基数、膨胀风险和人工确认契约 |
| `market/models.py` | MarketFrequency 与可追溯 MarketBar 统一行情模型 |
| `market/providers/tushare.py` | Tushare 日线、1m/5m 分钟线和实时快照，成功后先持久化 |
| `market/storage.py` | PostgreSQL executemany 批量 upsert、唯一去重、时间索引和查询 |
| `actions/contracts.py` | 人工确认、幂等键、默认拒绝和审计的外部动作注册表 |
| `security/data_masker.py` | 数据脱敏 + 频率限制 + 审计日志 |
| `security/network.py` | 数据库出站 DNS/IP 校验，私网默认拒绝并支持部署 allowlist |
| `security/api_access_policy.py` | YAML 基线 + PostgreSQL 动态策略原子快照、模板匹配、可信代理与 CIDR 黑白名单 |
| `security/sql_execution.py` | 权威方言解析、AST 只读校验、EXPLAIN 与有界执行唯一入口 |
| `security/sql_schema_validation.py` | 作用域感知的 SQL↔Schema 校验唯一实现：表索引、归属解析、置信度分层与结构化 issue，供 layer3 / execute_sql / generate_sql 共用；不依赖 `src.graph` |
| `failure_policy.py` | SQL/数据库 fail-closed 与 LLM/知识/处理器 fail-open 决策矩阵 |
| `mcp_client/client_manager.py` | MCP Client 独立连接栈、自动迁移、system/tenant/private 请求级工具过滤 |
| `mcp_client/server.py` | MCP Server（暴露 4 个工具，内部异常返回稳定脱敏协议） |
| `mcp/__main__.py` | `python -m src.mcp` 稳定启动入口，旧 `src.mcp_client` 入口复用同一实现 |

### ⑬ `src/db/` — 状态库迁移

`migrations.py` 在应用启动早期按编号扫描 `migrations/*.sql`，使用 PostgreSQL advisory lock
避免多实例并发，按文件事务执行，并在 `schema_migrations` 记录版本、文件名与按 LF 规范化计算的
checksum。执行器同时兼容历史 LF/CRLF 登记值，避免 Git 跨平台换行转换造成误报；真正的内容变更
仍然失败关闭。已知历史迁移被改写时只允许走版本、文件名、新旧 checksum 和后续修复文件均精确
匹配的白名单升级路径，且不覆盖原有审计记录。已登记迁移只在 DEBUG 输出逐项结果，INFO 使用
`applied_count / verified_count / legacy_repair_count` 聚合审计，已完成修复不重复产生 WARNING。
`013_tenant_identity_llm.sql` 为租户增加全局不可变 `code` 和租户内大小写敏感用户名约束，并建立平台 LLM 厂商/模型目录、租户命名连接、模型绑定和默认配置表。
`014_llm_catalog_reasoning.sql` 增加厂商能力表单定义，初始化 DeepSeek V4 Pro/Flash，并把旧 `deepseek-chat` 的租户绑定和默认值平滑迁移到 Pro 后删除旧目录项。
`007_api_access_policy.sql` 为动态策略和 CIDR 规则启用强制 RLS。API 策略加载属于必需启动步骤，
任一环境加载失败均阻断启动，防止数据库规则静默失效。
`009_automation.sql` 为自动化任务、运行记录和站内通知建表并启用租户/用户 RLS，
`010_automation_force_rls.sql` 强制表所有者也执行这些策略。
`015_knowledge_evolution.sql` 记录租户/用户隔离的已验证 SQL 观察次数，为 LEARNED_PATTERN 晋升提供幂等证据。
`016_knowledge_evolution_claims.sql` 增加晋升 Claim Token、超时接管、稳定记忆 ID 和错误摘要字段，防止多 Worker 重复晋升及失效 Worker 写回。
`017_knowledge_evolution_rls_repair.sql` 为曾执行早期 `015` 的数据库幂等补齐观察表索引、强制 RLS 和租户/用户身份 Policy。

## Skills 系统

`src/skill_manager.py` — system/tenant/private 受管目录扫描 + 复合标识 + 请求级身份过滤。匹配同名 Skill 时按 private > tenant > system 选择。内置仓库 Skill 可进程内执行；其他 Skill 必须通过 `skill_security.py` 验签，并由 `skill_runtime.py` 交给隔离 Worker，强制超时、资源、网络/文件、输入输出 Schema、脱敏和引用边界：
- `data-quality-check` — 空值/重复/异常检测
- `custom-report` — Jinja2 模板报告
- `data-profile` / `data-preparation` — 数据画像与非破坏式可审计数据准备
- `schema-linking` / `cross-source-fusion` — 词法模式映射与 JoinContract 跨源连接检查
- `statistical-analysis` / `timeseries-analysis` — 确定性统计、滚动回测和区间预测
- `document-evidence` / `artifact-export` — 可追溯文档证据与无文件写入的标准文本导出
- `sql-diagnostics` / `skill-validator` — SQL 静态诊断与 Manifest v2 离线校验
- `data-contract` / `schema-drift` — 显式数据契约校验与结构版本兼容性审查
- `dataset-diff` / `sampling-audit` — 主键数据集差异与可复现抽样代表性审计
- `distribution-drift` / `semantic-normalization` — 分布变化测量与显式语义规则规范化
- `provenance-audit` / `result-verification` — 来源变换链完整性与结构化结果质量核验
- `privacy-classifier` / `skill-evaluator` — 不回显原值的隐私分类与执行记录离线质量门禁
- `execution-trace-audit` / `workflow-state-diff` — 节点轨迹与执行前后、持久化状态一致性审计
- `prompt-injection-detector` / `secret-scanner` — 不可信指令检测与不回显原值的工程凭据扫描
- `reproducibility-audit` / `api-contract-drift` — 重复执行上下文复现与 OpenAPI 协议兼容性审查
- `file-ingestion-inspector` / `evidence-coverage-audit` — 文件摄取风险与关键结论独立证据覆盖门禁
- `numeric-integrity-audit` / `resilience-policy-audit` — 数值精度单位与超时重试幂等补偿策略核验
- `sql-optimization` — 基于 AST、Schema、索引和统计信息生成性能优化建议，明确不执行 SQL
- `feature-dev` — 开发流程指南
- `systematic-debugging` — 系统调试协议

仓库内三十三个 Skill 均包含中文 `SKILL.md`、`agents/openai.yaml`、严格输入输出 Schema、确定性
异步 `tools.py` 和固定 `eval/cases.yaml`；`custom-report` 额外保留只读 Jinja2 周报/月报模板资产。
默认网络权限为空，除 `data-quality-check` 为支持按列检查保留五十次有界调用预算外，其余单请求
工具预算为一次；数值、SQL、连接和引用结论必须由工具契约生成，不能由 LLM 自行补写。

## 前端

对话结果按图表/核心数值→数据详情→分析正文展示。`frontend/src/hooks/chatPresentation.ts` 只接收公开 answer_delta/answer_reset，并隔离并行和旧轮次；`useChat.ts` 将 data/chart/analysis_patch 写回部分结果，不等待 final 才渲染。`api/client.ts` 完整处理 UTF-8 分片，仅 done(status=complete) 为成功，提前断流按错误处理。`DataTable` 分离展示标签与原始键；`ChartPanel` 切换时保留 chart_plan 与 column_labels。缺失值保持 null，折线 connectNulls=true，不补零或插值。

`frontend/src/components/chatChartLayout.ts` 是聊天图表共用的纯响应式布局模块：`ChartPanel` 通过 `ResizeObserver` 观察实际容器，再独立调整多面板高度、窄屏紧凑刻度、标题/图例及饼图/热力色阶位置，不修改原选项、业务数据、分类、null 或坐标索引。紧凑刻度采用结果展示语言，提示框仍保留精确原值；移动样式不再覆盖计算高度。非流式 `/chat` 与公开最终结果保持相同的顶层语言、字段标签及摘要开关契约。

`ReasoningPanel` 现在只呈现公开业务过程摘要，默认折叠并按节点/调用分组，原始模型思维链始终隐藏。开关 chat_show_analysis_process 默认 false，由固定平台管理员通过运行参数面板热改，聊天请求不能覆盖；这与模型侧 reasoning_enabled 能力开关独立。语言优先显式要求、本轮问题、历史、界面，display_language 透传至分析提示词。设计与验收边界见 `spec/52-chat-presentation.md`，浏览器综合回归见 `tests/test_frontend/test_chat_presentation_browser.py`。

`frontend/` — React 18 + TypeScript 5 + Ant Design 5 + ECharts。登录固定输入 `tenant_code + username + password`，不展示公开注册；登录后按角色展示业务菜单。`AdminPage` 对 super_admin 展示租户、厂商能力字段编辑器和可物理删除的平台模型目录，并以精确确认文本物理删除非默认租户；对 tenant_admin 展示当前租户用户、命名连接和默认模型。`ChatPage` 只提交当前租户可见的连接与模型，并按模型能力展示推理开关和深度。`AutomationPage` 提供兼容任务说明、创建、暂停/恢复、立即运行、运行记录、删除和站内通知视图。`ReportWorkbenchPage` 负责自然语言计划预览、任务排队和权限/配额提示；`ReportViewerPage` 展示版本化动态 DSL、快照 Drilldown、运行节点日志和分享治理；`SharedReportPage` 只读取脱敏快照并通过短时授权会话访问分享 Drilldown。

### 报表工作台 — `src/reporting/`

报表级运行质量说明由 `profiling.is_runtime_quality_unit()` 识别，独立保留为说明单元，不与数据查询去重合并；`report_build_relations` 在全部查询与运行期补查完成后调用 `summarize_runtime_quality()`，从本轮有界样本、执行/授权状态和 `RelationEvidence` 回填质量章节，不再用独立 Schema 直答描述整张报表。普通字段说明与数据质量指标查询不改变路由。`runner` 保留模型已披露的限制，多源各级样本裁剪在分析前传递 `truncated`，月度样本不能宣称逐日全量覆盖。SQL 阶段二复核必须保留类型与采样格式，类型错误的准确修法随结构化 issue 进入生成重试。

`planner.py` 将自然语言解析为可确认的 `ReportPlan`：调度、次数、有效期和数据源由确定性规则抽取，业务分析单元优先由 `report.plan` 结构化 LLM 拆解（经租户模型绑定，按用户书写的编号章节划分），模型不可用或尝试越权数据源时回退规则拆分并在 `warnings` 标注降级；`store.py` 持久化 Job/Run/Step/Version/Share、配额策略和访问审计，并按 PostgreSQL/SQLite 方言选择认领策略；`runner.py` 与 `src/graph/subgraphs/report_workbench.py` 执行多分析单元：分析单元绑定了数据源则单源执行；未绑定时由 `report.route` 结构化 LLM 依据各源表清单判定命中的数据源，只对命中源执行，判定无命中则跳过该单元并说明原因（不计入失败）；路由模型不可用才退回全源检索；`relations.py` 只消费授权投影和有界样本生成关系证据，阻断低匹配率或多对多膨胀；`document.py` 校验动态页面 DSL 和 ECharts 白名单；`drilldown.py` 仅允许服务端签发的快照过滤；`sharing.py` 负责 token、授权码和用户范围访问控制；`quota.py` 执行菜单能力、周期次数、并发、查询、Token 与分享限额。

任务创建通过 `ReportStore.create_job_with_quota()` 将幂等检查、配额预占和写入纳入同一事务，仅首次创建才入队。运行线程包含显式租户、用户和运行 ID；取消落库后在统一清理路径释放原周期并发槽。单次资源上限不与月度累计量混用。生产权限以持久化记录为准，权限库异常失败关闭，不能被 Worker 内旧授权覆盖遮蔽。存量任务的完整调度与恢复仍见功能清单中的待开发项，不由内存 asyncio.Task 替代。

`frontend/src/components/report/ReportPolicyEditor.tsx` 提供租户管理员的用户额度编辑入口；平台管理界面不展示该业务配置。`frontend/src/components/EChartsView.tsx` 统一注册项目支持的图表类型并由聊天图表和报表渲染器延迟加载。浏览器行为测试位于 `tests/test_frontend/test_report_browser.py`，安装 `.[browser]` 后使用 `RUN_BROWSER_TESTS=1` 显式运行；测试启动独立 Vite 进程并拦截业务 API，不访问生产后端。

## 快速上手

1. `src/main.py` → `src/bootstrap.py` → `src/api/routes/` — 启动与 API 入口
2. `src/app_context.py` → `src/security/tenant_policy.py` — 应用依赖与租户策略
3. `src/api/auth.py` → `src/api/routes/admin.py` — 租户身份、用户自治和管理员边界
4. `src/api/routes/llm_admin.py` → `src/llm/tenant_config.py` — LLM 目录、连接和默认选择
5. `src/graph/workflow.py` — 流水线组装
6. `src/graph/state.py` — 状态定义
7. `src/graph/nodes/generate_sql.py` — 核心 LLM 调用
8. `spec/README.md` — 技术规格索引
9. `features/README.md` — 功能清单索引

## 关键设计模式

- **分类重试**：SQL/字段/EXPLAIN 语义错误 → generate_sql；连接瞬态错误 → 原 SQL 重试 execute_sql；配置/权限错误终止
- **指标目录分流**：纯目录问法不要求时间；表式/宽表字段式先排除主键、编码、排序和坐标，再让 LLM 仅在真实数值字段白名单内复检和主题化；唯一行式目录只查询 DISTINCT code/name，多候选请求目标表
- **计算与解读分层**：数据处理器负责确定性数值计算但不得提前终止分析；LLM 负责关联指标、业务影响、待验证原因和条件化建议，模型不可用时用显式“待验证”规则回退
- **有限证据补查**：分析模型只声明证据状态和自然语言补查问题；单源普通分析最多补查两轮，每轮一个问题，完整复用 SQL 安全链，失败保留主查询结果
- **确定性图表资格**：元数据、直接问答和无数据不画图；单行单值展示核心值；两行以上时间/分类加数值默认绘图；模型推荐 `table` 仍需列语义复核，失败原因进入公开响应
- **表名幻觉收敛**：完整物理表白名单独立于可裁剪 Schema，表名比对复用 `security/sql_schema_validation.py` 的统一表索引与归属解析；幻觉不回注完整失败 SQL，初次生成后最多纠正一次
- **应用级依赖容器**：每个 FastAPI 应用持有独立 `AppContext`；`get_settings()` 无绑定时不隐式创建 Context，兼容 `get_*()` 委托当前 Context，异步资源并发单次初始化并按创建逆序关闭
- **租户策略集中化**：`TenantPolicy` 统一认证门禁、数据源/知识隔离、身份校验和三级作用域写权限，业务模块不直接读取 `settings.multi_tenant`
- **租户 LLM 失败关闭**：所有对话必须解析当前租户启用的命名连接和模型；显式跨租户、停用或缺失选择返回冲突，不回退其他租户或平台全局凭证
- **LLM 协议扩展**：Provider 目录保存协议元数据，`openai_compatible` 映射到统一 OpenAI Adapter；增加同协议厂商或模型只需配置目录，新增协议才实现并注册 Adapter
- **异常回退可见性**：可恢复回退保留原返回契约但必须记录完整堆栈；Provider 等基础设施故障不得伪装成空数据
- **日志粒度以"可回测推理"为准**：INFO 只留主要节点的边界、关键决策（路由/选源）、结论（生成/校验/执行/发布）与错误；每次调用都会触发的调用内基础设施细节（LLM provider 解析链、可用性检查、方言重写、逐列数值提取）一律 debug。常规读请求的访问日志由 `api_access.default_access_log` 收敛为 `none`，异常仍走 `API 访问异常`，登录/注册/分享/数据源与 Schema 变更等安全路由保留 `security`/`audit` 审计。单次报表运行由此减少约 35% 的 INFO 记录
- **覆盖率门禁**：`coverage run -m pytest -q -m "not live_llm"` 后执行 `coverage report`，branch coverage 最低 67%，生产模块禁止 0% 覆盖
- **多源失败隔离**：每个 worker 独立执行 Schema/SQL/Layer3/EXPLAIN/Execute，不可达来源返回来源级错误
- **跨源列契约**：统计、单元模型与页面从全部有界行提取有序联合字段；只允许已知同义别名或可核验 SQL 投影别名恢复。列数和 dimension/metric 角色相同不代表同一指标，异义字段保留原名与来源，缺位不补零。
- **报表执行证据**：年度范围继承到发散单元，查询去重包含问题和路由来源；禁用旧补查标记必须在 AnalysisState 中声明，保证穿过真实编译图。质量章节在所有数据单元与关系处理后汇总，不能独立询问模型是否执行过查询。
- **分析预算与回退**：模型样本按来源交错，并提供确定性样本日期范围、来源计数和 SQL。最终结果分析不被旧前三行摘要覆盖；JSON 契约异常最多在同一超时预算内重试一次，失败仍带堆栈回退，不静默丢弃额外对象。
- **深度并发与关闭**：ClickHouse 共享只读客户端禁用自动 session ID；DataSourceRegistry 经 AppContext 关闭，等待活动连接及缓存失效产生的释放任务，不能依赖事件循环结束后的析构。
- **最终 SQL 展示**：多源 worker 返回 execute_sql 方言重写/权限注入后的 SQL，响应通过 sql_statements 按来源展示
- **有界知识工具循环**：`invoke_structured`/`invoke_text`/`stream_prompt` 统一经 `knowledge_loop`，模型不请求工具时仍只有原一次调用、零检索；工具回合 ≤3、真实检索 ≤6，每个 `tool_call`（含未知工具、非法参数、`invalid_tool_calls`）都必须有匹配 `tool_call_id` 的 `ToolMessage`。权限失败关闭，检索故障按 `FailureDomain.KNOWLEDGE` 降级。工具交互空间按真实序列化计数在 `build_budgeted_prompt(reserve_chars=...)` 中预留：包含工具定义、一条有界工具结果（与 `RESULT_TOTAL_CHARS` 同源的 6000 字符）与协议开销，保证证据不会被预算裁空；连一条有用证据都放不下时不挂工具，退化为一次普通调用而不是事后裁剪；`bind_tools` 后重新套回原 `with_config` 配置，provider 续轮消息保留 `additional_kwargs`（DeepSeek `reasoning_content`）
- **知识检索范围与后台身份**：普通请求只绑定数据源与请求白名单（`bind_request_knowledge_scope`），身份始终取自真实请求上下文并在执行期复核；后台任务用 `use_trusted_identity` 同时绑定认证 ContextVar 与循环配置，退出时精确 reset，无可信身份只禁用检索而不采用默认租户或平台身份
- **并行流隔离**：SSE 使用 LangChain run_id 派生 stream_id，前端按调用实例缓冲 token；原始 reasoning_content 仅做服务端受控诊断。挂工具的模型回合带 `knowledge_tool_round` 标记：SSE 先缓冲，在 `on_chat_model_end` 按输出是否含 `tool_calls` 决定释放或丢弃，`llm_start`/`llm_end`/`final`/`done` 语义不变；`KnowledgeRoundReset` 供节点内消费方丢弃中间正文
- **安全阻断**：layer3 使用 sqlglot AST 只读白名单，阻断 DDL/DML、SELECT INTO 和状态变更函数；表/列解析异常失败关闭
- **列引用归属校验**：列白名单是所有表列名的并集，只看列名会漏掉跨表错配（`created_at` 只存在于 A 源 orders，却写成 B 源 finance_invoices 的别名前缀 `i.created_at`，能通过白名单直到 EXPLAIN 打库才报错）。校验必须解析别名后按表归属判断，且前移到 `layer3_validate`——幻觉列在那里失败才能把可用列清单喂回重试，`execute_sql` 的同类校验只作最后防线。判定逻辑唯一实现于 `security/sql_schema_validation.py`（按 `(datasource, table)` 建索引、作用域感知），产出分级 `ValidationIssue`：high 拦截重试；low 放行留痕，但只有 `SELECT *` 会产出低置信 `ValidationIssue` 并计入指标，CTE 限定符 / 含 CTE 零命中 / 空列集表 / 命名空间前缀等"无法判定"分支只写 warning 日志并 `return []`——它们不产出 issue、不进入 `data_agent_sql_schema_validation_total`，排查"为什么没拦住"时必须看日志而不是只查指标。基准按数据源取权威快照：layer3 阶段一用 `relevant_tables` 零 IO 校验，出现 high 置信问题或阶段一无基准时才进入阶段二，用 `get_or_fetch_schema(ds, user_query="")` 全量快照复核后定拦截，消除裁剪假阳性；schema 幻觉重试不回注失败 SQL，只按表回喂可用列清单（issue 上限 3、每表列上限 10）
- **后处理层不得改坏正确输入**：`sql_rewriter` 的 sqlglot 归一化会把 ClickHouse 的 `toStartOfMonth(x)` 渲染回 `dateTrunc('MONTH', x)`，而后者只接受 DateTime，Date32 列直接报 code 43——等于把模型按方言提示写对的函数改坏。ClickHouse 必须在其后再做一次 `dateTrunc → toStartOfX` 回退；凡是自加的后处理层，都要验证它不破坏正确输入
- **提示预算跟随模型窗口**：`context_budget` 不是与模型窗口无关的常数。固定 4400 的 schema 上限会把上百张表的库裁到只剩几张，目标表列名进不了 Prompt，模型只能按惯例猜列名（曾导致 `created_at` 不存在）。schema 段按"能装下整库表结构"设置，总预算需同时容纳 schema 与业务上下文，否则会把问题从一段挤到另一段
- **高权限连接告警**：DataSourceRegistry 识别 Oracle `SYS/SYSTEM` 和 SQL Server `sa` 时记录 warning 但继续连接；数据库账号权限不改变 layer3 的只读失败关闭策略
- **向量过滤精确化**：Milvus LIKE 仅缩小候选集，解析 metadata 后再次精确校验，搜索、计数和删除共享同一过滤语义
- **阻塞 SDK 隔离**：Milvus 集合操作及嵌入模型导入统一通过工作线程执行，RPC 有限超时；取消写任务等待线程退出后才释放写锁。规范表结构先发布，派生索引刷新有独立超时和异常收束。Chroma 的 `search`/`get_by_id`/`get_by_filter`/`count` 同样 `asyncio.to_thread` offload，否则同步 SDK 会占住事件循环，`asyncio.wait_for` 无法生效；Milvus 只读路径进一步拆分取消策略：`_run_on_collection_read`/`_run_abandonable_read` 取消后立即返回，在途线程受 SDK `_RPC_TIMEOUT_SECONDS` 约束并由 `_consume_task_result` 收束异常，工具期限因此真正有界；写入路径仍用 `_run_sync` 等在途调用退出后才释放写锁
- **认证暂时不可用**：`AuthContext` 区分服务故障和明确未认证；超时、断网、5xx 保留当前路由显示重试，不清 Cookie。只有明确未认证结果进入登录页，不能用保存的用户信息绕过服务端校验。
- **生产密钥门禁**：生产禁止默认数据库凭证、默认凭证主密钥和临时 JWT；Docs、Redoc、OpenAPI 同时关闭
- **API 中间件安全**：访问策略先解析可信客户端 IP 并执行 deny/allow，再把认证模式下传纯 ASGI Auth；CORS 默认拒绝跨域；生产 HTTPS 启用 CSP/HSTS
- **访问策略混合存储**：公开/可选认证仅由版本化 YAML 声明；数据库动态策略只能保持或收紧认证，IP 规则写入后原子刷新 AppContext 快照
- **出站地址失败关闭**：数据库目标解析为私网、回环或特殊地址时默认阻断；ClickHouse 探针与客户端固定复用已校验 IP
- **授权候选发现**：未选择数据源时仅把当前用户有权访问的候选交给模型，显式越权与权限服务异常均失败关闭
- **版本化迁移**：启动时用 advisory lock + checksum + 单文件事务应用 SQL，生产失败停止启动
- **容器部署边界**：`docker-compose.example.yml` 只发布前端端口，Nginx 同源代理后端；PyPI/npm 默认官方源并允许构建参数覆盖；实际 Compose/配置文件忽略，PG/Redis/Chroma/Skills/日志使用宿主机持久化目录
- **状态持久化分层**：checkpoint 只保存轻量 `conversation_history/messages/previous_turn_snapshot`；权限、Schema、结果、分析、图表和响应使用 `UntrackedValue`
- **状态上下文分组**：请求、权限、路由、执行上下文只保存轻量元数据；兼容期保留扁平字段，每轮由 `prepare_turn` 重建，禁止真值回退复活旧状态
- **Windows 异步兼容**：`src.main` 为 Uvicorn 显式创建 SelectorEventLoop，保证 psycopg `AsyncPostgresSaver` 可持久化
- **会话逐轮恢复**：每轮经过脱敏的 `final_response` 只写 `query_history.final_result` JSONB；前端逐轮消费，checkpoint 不复制数据与图表
- **会话恢复回退**：逐轮 JSONB 是富结果权威来源；Checkpointer 只补轻量摘要和消息，旧/缺失状态回退贫化摘要/SQL
- **单一路径工作流**：执行路由统一走条件边，避免并行分支状态丢失
- **子图复用**：多数据源 worker 通过子图复用主图节点和条件路由语义，合并展示使用固定分析/图表边；两者均传递父级 RunnableConfig 的 metadata/tags
- **能力子图分层**：文件/市场研究/动作在路由后直接进入子图；预测/报告复用 SQL 主链后再进入子图；所有能力统一进入 build_response 和 AnalysisArtifact
- **多源强类型边界**：调度器通过 `SourceQueryRequest/SourceQueryResult/MultiSourceResult` 校验来源覆盖、权限和结果统计，worker 只接收显式白名单状态
- **结果状态统一**：`success/status/source/error_code/error_message` 是所有 SQL、MCP、直接回答和多源路径的共同契约；内部推理与数据库异常不进入 SSE、历史或会话恢复
- **Prompt 可扩展**：新增能力先注册 PromptDefinition 和输出模型，再由节点声明 PromptSection 的优先级、最低保留量和上限；解析失败只能走显式兼容回退
- **三级扩展隔离**：Skill/MCP 使用 system/tenant/private 统一作用域；系统写入仅 super_admin，租户写入仅 tenant_admin/super_admin，个人资源仅本人
- **Skill 两阶段激活**：意图阶段记录包含表触发项的候选集，Schema 阶段按真实表名收敛最终资源 ID；显式选择始终重新校验身份可见性
- **Skill 供应链与运行隔离**：受管 Skill 默认要求可信 Ed25519 签名；非内置 tools.py 不在主进程导入，Worker 强制超时、资源、文件/网络权限、输出大小和契约校验
- **跨轮结果索引**：`previous_turn_snapshot` 只保存来源、SQL 和可用性标记；明确 meta 追问校验数据源后从 HistoryStore 恢复
- **统一 SQL 安全执行**：Graph Layer 3/4、执行节点和 DB Tools 均委托 `security/sql_execution.py`，Registry 方言覆盖调用方提示
- **Oracle 集合运算字符集**：统一 SQL 边界将 Registry 已发布的 `resolved.schema` 传入 `rewrite_sql`，按作用域追踪 CTE/派生表输出；仅对明确混合普通字符集与国家字符集的同位置文本列补 `TO_NCHAR`，保留指标、别名和过滤。没有权威类型时不猜测，继续 EXPLAIN/重试；执行与状态写回使用同一份修正 SQL，规则不额外查询 Schema、不调用模型。
- **向量后端一致性**：Chroma/Milvus/pgvector 共享等值、`not:key`、`$ne` 过滤 DSL，空过滤删除统一阻断
- **节点级模型降级**：轻量任务本地模型不可用 → 确定性规则；远程任务需显式授权；仅开发/测试环境允许 PG 不可用时使用 MemorySaver，生产环境必须持久化 Checkpointer
- **行情先持久化**：MarketDataProvider 请求成功后必须先写入 `market_bars`，写入失败不向分析层返回数据
- **外部动作安全边界**：动作默认拒绝，必须人工确认和幂等键；本项目不实现自动交易
- **受控处理摘要**：`decision_summary` 只由来源、状态、行数、重试次数和能力数量生成并限制长度；模型原始 `reasoning_content` 不进入响应、SSE、历史或会话恢复
- **数据源租户复合键**：外挂数据源持久化使用 `(tenant_id, name)` 唯一键，运行时 Provider/Registry 使用复合缓存键；单租户模式保留旧数据兼容读取
- **双写补偿**：长期记忆 PostgreSQL 与 VectorStore 发生部分失败时执行回滚或写入 `pending_vector_sync`，删除操作使用可重放 tombstone
- **Schema 刷新保护**：过期 Schema 先刷新、成功后再清理旧条目；刷新失败保留旧缓存并等待下一轮重试；隔离模式的条目 ID 包含租户命名空间
- **自动化成功语义**：通知任一渠道返回非 `success` 时任务记为失败，不推进成功基线；月度调度按日历月末日截断
- **统一任务计划与分析产物**：路由节点写回 `task_plan`；最终响应同时提供兼容旧字段和 `artifact`，产物使用 `AnalysisArtifact`、证据和复现信息。
- **统一结构化 Prompt 调用**：新增能力必须注册 `PromptDefinition` 与输出模型，节点通过 `src/llm/invocation.py` 统一预算、模型调用和 Pydantic 解析。
- **固定评测门禁**：`tests/fixtures/graph_benchmark.json` 离线覆盖路由、安全和 Artifact；真实本地 SQLite 双源验收默认运行，外部数据源与 LangSmith 仅在显式开关下运行。
- **启动托管密钥**：`datasource/secret_provider.py` 在生命周期启动前读取 Vault KV v2/只读 JSON，只允许敏感字段注入；失败关闭，不降级到环境旧密钥。默认 environment 模式保持兼容。
- **存储凭证轮换**：`datasource/credential_manager.py` 写入带 key_id 的 v3 密文并兼容 v2；`python -m src.datasource.rotate_credentials` 默认核验，停机后 `--apply --offline-confirmed` 才在单事务中更新数据源和租户模型凭证。部署契约见 `spec/31-secret-custody.md`。
- **指标治理**：`knowledge/metric_catalog.py` 管理租户指标的不可变版本、审核及未来生效；API `/metric-catalog` 与前端 `MetricsPage.tsx` 仅给租户管理员业务写权限。显式 `@metric(code@version)` 在 Schema 节点解析为已批准知识证据，未命中失败关闭；迁移 `019_metric_catalog.sql`。
- **脱敏可视化**：`security/data_masker.py` 在实际脱敏时产出 `masking` 字段摘要；单源、多源与补查保留它，`build_response` 和 HistoryStore 传递它，`DataTable`/`ResultCard` 只使用该元数据展示标识。
- **图片资产**：`knowledge/image_assets.py` 校验真实图像、移除元数据并限制像素/字节，`mcp_agent` 重验附件拥有者后通过统一 LLM 工厂调用显式支持视觉的租户模型；工具结果与 `AnalysisArtifact` 仅传递资产和证据，不传图片字节。
- **报表调度**：`reporting/scheduler.py` 接入 bootstrap，认领与心跳依赖数据库，实际运行使用任务专属 PostgreSQL 会话锁；发布复用持锁连接并重新校验权限。`store.py` 将运行配额租约与预占事务化，恢复时只释放原周期并发槽。
- **实时钻取**：`reporting/live_drilldown.py` 只处理私有表 `report_live_queries` 中的服务端模板；公开 DSL 只携带随机引用，分享响应移除 live 定义。SQL 执行前当前 ACL、字面量参数和预算校验，执行后投影脱敏。
- **跨源样本聚合**：`reporting/aggregation.py` 以明确共同维度分别统计来源，禁止原始行连接膨胀，结果作为独立表格进入页面；不把样本或非可加业务指标伪装为全量总额。
- **页面管理**：查看器通过生命周期摘要管理已过期页面，延期/永久能力由租户授权；不会自动扩大分享期限。新迁移 020～022 覆盖私有查询、调度恢复字段和租户 RLS 边界；审计清单见 `features/24-tenant-governance-audit.md`。
- **平台与业务分权**：平台 `super_admin` 不再拥有报表业务能力，旧覆盖不能恢复；仅无请求的后台 `(tenant_id=0,user_id=0)` 保留基础设施通道，实际报表执行仍绑定业务拥有者。迁移 024 统一报表、知识和 MCP 的 RLS 边界。
- **共享查询额度**：`security/query_quota.py` 使用 PostgreSQL UTC 小时窗口和租户事务锁同时消费用户/租户额度；`data_masker.check_rate_limit` 为异步门面。`api/routes/query_quota.py` 与 `QueryQuotaEditor.tsx` 管理租户软额度，平台硬上限来自统一 Settings，数据库故障失败关闭。
- **知识应急访问**：`security/emergency_access.py` 与 `api/routes/emergency_access.py` 提供单文件、指定超管、限时只读授权及撤销；`EmergencyKnowledgePanel.tsx` 为拥有者与平台提供不同操作。普通检索不使用授权，读取前必须验证当前归属并提交审计。
- **MCP 安全停用**：`security/mcp_safety.py` 只维护独立阻断表，不读改租户连接；`MCPSecurityPanel.tsx` 仅在平台工作区提供。`client_manager.py` 名称解析绑定当前身份，工具调用每次重验共享状态，阻断不可用同名重建绕过。
