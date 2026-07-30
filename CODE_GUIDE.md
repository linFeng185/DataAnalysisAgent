# DataAnalysisAgent — 代码导航指南

## 项目概述

LLM 驱动的数据分析智能体。用自然语言提问，自动完成：轮次状态初始化 → 意图识别 → 表结构检索 → SQL 生成 → 本地安全校验 → 目标库 EXPLAIN → 执行 → 分析 → 图表生成 → 响应组装。

**技术栈**：FastAPI + LangGraph + LangChain + sqlglot + SQLAlchemy + React + TypeScript。

## 目录结构

```
├── src/
│   ├── api/              ① Web 接口层 — 访问策略/领域路由/纯 ASGI 认证/安全头 + SSE 流式
│   ├── graph/            ② 核心流水线 — LangGraph 主图 + 可复用 SQL 子图
│   │   ├── context.py       请求/权限/路由/执行四组轻量上下文适配
│   │   ├── contracts.py     任务计划与多数据源请求/结果 Pydantic 契约
│   │   └── nodes/           状态准备、SQL 主链、直接回答与多源节点
│   │   └── subgraphs/       SQL 复用、结果展示及文件/研究/预测/报告/动作能力子图
│   ├── llm/              ③ LLM 调用层 — Provider 注册表 + 租户连接选择 + 适配器 + 版本化 Prompt
│   │   └── adapters/         模型适配器
│   ├── datasource/       ④ 数据源管理 — 注册/发现/Schema/凭证加密
│   │   └── providers/        数据源提供者
│   ├── connectors/       ⑤ 数据库连接器 — 自注册方言与统一运行时策略
│   ├── knowledge/        ⑥ 知识库 — 三范围治理 + 标签 + 连接级缓存 + 文档摄取
│   ├── memory/           ⑦ 记忆系统 — 会话持久化 + 上下文裁剪 + 历史
│   ├── automation/       ⑧ 主动洞察与定时报告 — PG 调度 + 重授权执行 + 通知分发
│   ├── tools/            ⑨ 分析工具 — 统计、预测与跨资产分析
│   ├── market/           ⑩ 行情 Provider 与 PostgreSQL 持久化（当前 Tushare/A 股）
│   ├── actions/          ⑪ 受控外部动作（人工确认、幂等、审计）
│   ├── security/         ⑫ 安全模块 — API/IP 策略 + SQL 统一执行 + 脱敏 + 限流 + 审计 + 出站策略
│   ├── db/               ⑬ 状态库基础设施 — 版本化迁移 + URL 工具
│   ├── mcp_client/       ⑭ MCP 集成 — 客户端管理 + 工具暴露
│   ├── app_context.py        应用级依赖容器 + ASGI 请求绑定 + 资源关闭
│   ├── config.py             配置管理 (pydantic-settings)
│   ├── bootstrap.py          分阶段启动/关闭编排
│   ├── skill_manager.py      技能发现、作用域与激活
│   ├── skill_security.py     Skill 包摘要与 Ed25519 验签
│   ├── skill_runtime.py      Skill 契约、资源和隔离执行入口
│   └── skill_worker.py       非内置 Skill 子进程 Worker
├── frontend/                 React SPA (Vite + Ant Design + ECharts)
│   ├── Dockerfile               Node builder + Nginx runtime
│   └── nginx.conf               SPA、API 代理和 SSE 配置
├── skills/                   system 内置 Skills
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
  ├─ layer3_validate     委托统一 SQL 服务做 AST 只读安全校验
  ├─ layer4_explain      统一 SQL 服务按 Registry 权威方言执行 EXPLAIN
  ├─ execute_sql         统一有界执行（权限 / 限流 / 脱敏 / 审计）
  ├─ analyze_result      统计计算 + LLM 洞察 + Skill 输出扩展
  │    ├─ 预测 → forecast_subgraph（回测/区间/模型卡）
  │    └─ 报告 → report_subgraph（custom-report 真实执行）
  ├─ generate_chart      默认 SQL 分析的 ECharts 配置生成
  └─ build_response      响应组装 + 轻量会话历史 + HistoryStore 富结果
```

## 各模块说明

### ① `src/api/` — Web 接口层

| 文件 | 职责 |
|------|------|
| `routes/__init__.py` | 组合各领域 APIRouter，并保留旧模块导出兼容 |
| `routes/*.py` | chat、datasource、schema、session、mcp、knowledge、skills、management、admin、llm_admin、access_policy 领域端点 |
| `routes/automation.py` | 自动化任务 CRUD、立即运行和用户私有站内通知 |
| `schemas.py` | Pydantic 请求/响应模型 |
| `streaming.py` | SSE 流式（13 种事件类型，LLM 调用按 stream_id 隔离） |
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
| `nodes/generate_sql.py` | LLM SQL 生成（对话历史注入） |
| `nodes/layer3_validate.py` | sqlglot AST 只读白名单与危险语句阻断 |
| `nodes/execute_sql.py` | SQL 执行（空 SQL 跳过保护） |
| `nodes/multi_source.py` | 契约化多源 worker、并行执行、维度/指标列对齐和来源失败隔离 |
| `nodes/analyze_result.py` | 统计 + LLM 分析 |
| `nodes/build_response.py` | 响应组装、最终 SQL 列表 + 轻量 checkpoint 历史 + 富结果持久化 |

### ③ `src/llm/` — LLM 调用层

`client.py`（兼容工厂 + local/remote/none 任务路由）+ `provider_registry.py`（协议别名与 Provider 注册）+ `tenant_config.py`（租户命名连接、默认值、凭证解密和请求级 ContextVar）+ `invocation.py`（统一调用出口）+ `adapters/` + `prompts.py` + `prompt_budget.py` + `output_contracts.py`。`routes/llm_admin.py` 由 super_admin 维护厂商/模型目录，由 tenant_admin 维护当前租户连接和默认值；Chat/SSE 入口解析 `llm_connection_id + model_id` 后绑定请求上下文，多租户未配置时失败关闭，单租户保留 Settings 兼容回退。Prompt 通过稳定 ID/多版本注册，支持激活和回滚；System 与动态上下文共享字符预算，所有业务调用统一附加 Prompt/任务追踪 metadata，节点输出优先由 Pydantic 校验；默认只有 `generate_sql` 可调用远程模型，轻量节点优先 `LOCAL_LLM_*`。

### ④ `src/datasource/` — 数据源管理

`registry.py`（全局 Provider、Oracle/ClickHouse 方言适配、`resolve_or_none` 不抛异常；ClickHouse 按 `extra_params.connect_timeout` 做 TCP 建连探针）+ `credential_manager.py`（无源码默认主密钥、每次随机 salt 的 `v2:salt:token` Fernet 密文、历史 token 兼容 + 环境变量凭证）+ `setup.py`（SQLite 演示库）。

### ⑤ `src/connectors/` — 数据库连接器

6 种方言通过 `connectors/registry.py` 自注册：ClickHouse / MySQL / PostgreSQL / SQLite / Oracle / MSSQL。超时、EXPLAIN、探针和 Engine 参数由各 Connector 封装；ClickHouse 在网络调用前校验全部解析地址并固定使用已验证 IP；非 SQLite 连接 URL 通过 SQLAlchemy `URL.create()` 保存，字符串展示默认隐藏密码。

### ⑥ `src/knowledge/` — 知识库

| 文件 | 职责 |
|------|------|
| `datasource_cache.py` | 按连接指纹共享数据库元数据，支持本地 JSON / Redis 配置切换 |
| `schema_manager.py` | 连接级精确缓存 + VectorStore 语义索引 + 文档/DB 内省回退 |
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
| `session_archive.py` | 会话归档 |

### ⑧ `src/automation/` — 主动洞察与定时报告

`AutomationStore` 使用 PostgreSQL `SKIP LOCKED` 认领到期任务；`ScheduledAnalysisRunner` 每次运行重新检查
数据源、行列权限和只读 SQL；`NotificationDispatcher` 的 SMTP/飞书/Slack 只读取服务端配置，站内通知持久化到 PostgreSQL。

### ⑨~⑫、⑭ — 工具/行情/动作/安全/MCP

| 模块 | 职责 |
|------|------|
| `tools/analyzer.py` | 描述性统计/趋势/Z-score 异常/集中度/Pearson 相关 |
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
| `failure_policy.py` | SQL/数据库 fail-closed 与 LLM/知识/处理器 fail-open 决策矩阵 |
| `mcp/client_manager.py` | MCP Client 独立连接栈、自动迁移、system/tenant/private 请求级工具过滤 |
| `mcp/server.py` | MCP Server（暴露 4 个工具） |

### ⑬ `src/db/` — 状态库迁移

`migrations.py` 在应用启动早期按编号扫描 `migrations/*.sql`，使用 PostgreSQL advisory lock
避免多实例并发，按文件事务执行，并在 `schema_migrations` 记录版本、文件名与 checksum。
`013_tenant_identity_llm.sql` 为租户增加全局不可变 `code` 和租户内大小写敏感用户名约束，并建立平台 LLM 厂商/模型目录、租户命名连接、模型绑定和默认配置表。
`007_api_access_policy.sql` 为动态策略和 CIDR 规则启用强制 RLS。API 策略加载属于必需启动步骤，
任一环境加载失败均阻断启动，防止数据库规则静默失效。
`009_automation.sql` 为自动化任务、运行记录和站内通知建表并启用租户/用户 RLS，
`010_automation_force_rls.sql` 强制表所有者也执行这些策略。

## Skills 系统

`src/skill_manager.py` — system/tenant/private 受管目录扫描 + 复合标识 + 请求级身份过滤。匹配同名 Skill 时按 private > tenant > system 选择。内置仓库 Skill 可进程内执行；其他 Skill 必须通过 `skill_security.py` 验签，并由 `skill_runtime.py` 交给隔离 Worker，强制超时、资源、网络/文件、输入输出 Schema、脱敏和引用边界：
- `data-quality-check` — 空值/重复/异常检测
- `custom-report` — Jinja2 模板报告
- `feature-dev` — 开发流程指南
- `systematic-debugging` — 系统调试协议

## 前端

`frontend/` — React 18 + TypeScript 5 + Ant Design 5 + ECharts。登录固定输入 `tenant_code + username + password`，不展示公开注册；登录后按角色展示业务菜单。`AdminPage` 对 super_admin 展示租户与平台 LLM 目录，对 tenant_admin 展示当前租户用户、命名连接和默认模型；`ChatPage` 只提交当前租户可见的连接与模型。`AutomationPage` 提供任务创建、立即运行、删除和站内通知视图。

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
- **应用级依赖容器**：每个 FastAPI 应用持有独立 `AppContext`；兼容 `get_*()` 只委托当前 Context，异步资源并发单次初始化并按创建逆序关闭
- **租户策略集中化**：`TenantPolicy` 统一认证门禁、数据源/知识隔离、身份校验和三级作用域写权限，业务模块不直接读取 `settings.multi_tenant`
- **租户 LLM 失败关闭**：多租户对话必须解析当前租户启用的命名连接和模型；显式跨租户、停用或缺失选择返回冲突，不回退其他租户凭证；单租户无命名连接时才允许 Settings 兼容路径
- **LLM 协议扩展**：Provider 目录保存协议元数据，`openai_compatible` 映射到统一 OpenAI Adapter；增加同协议厂商或模型只需配置目录，新增协议才实现并注册 Adapter
- **异常回退可见性**：可恢复回退保留原返回契约但必须记录完整堆栈；Provider 等基础设施故障不得伪装成空数据
- **覆盖率门禁**：`coverage run -m pytest -q -m "not live_llm"` 后执行 `coverage report`，branch coverage 最低 67%，生产模块禁止 0% 覆盖
- **多源失败隔离**：每个 worker 独立执行 Schema/SQL/Layer3/EXPLAIN/Execute，不可达来源返回来源级错误
- **跨源列契约**：按全部结果行识别 dimension/metric 角色，角色序列兼容时按位置统一任意数量列；冲突时保留原字段
- **最终 SQL 展示**：多源 worker 返回 execute_sql 方言重写/权限注入后的 SQL，响应通过 sql_statements 按来源展示
- **并行流隔离**：SSE 使用 LangChain run_id 派生 stream_id，前端按调用实例缓冲 token；原始 reasoning_content 仅做服务端受控诊断
- **安全阻断**：layer3 使用 sqlglot AST 只读白名单，阻断 DDL/DML、SELECT INTO 和状态变更函数；表/列解析异常失败关闭
- **高权限连接告警**：DataSourceRegistry 识别 Oracle `SYS/SYSTEM` 和 SQL Server `sa` 时记录 warning 但继续连接；数据库账号权限不改变 layer3 的只读失败关闭策略
- **向量过滤精确化**：Milvus LIKE 仅缩小候选集，解析 metadata 后再次精确校验，搜索、计数和删除共享同一过滤语义
- **生产密钥门禁**：生产禁止默认数据库凭证、默认凭证主密钥和临时 JWT；Docs、Redoc、OpenAPI 同时关闭
- **API 中间件安全**：访问策略先解析可信客户端 IP 并执行 deny/allow，再把认证模式下传纯 ASGI Auth；CORS 默认拒绝跨域；生产 HTTPS 启用 CSP/HSTS
- **访问策略混合存储**：公开/可选认证仅由版本化 YAML 声明；数据库动态策略只能保持或收紧认证，IP 规则写入后原子刷新 AppContext 快照
- **出站地址失败关闭**：数据库目标解析为私网、回环或特殊地址时默认阻断；ClickHouse 探针与客户端固定复用已校验 IP
- **授权候选发现**：未选择数据源时仅把当前用户有权访问的候选交给模型，显式越权与权限服务异常均失败关闭
- **版本化迁移**：启动时用 advisory lock + checksum + 单文件事务应用 SQL，生产失败停止启动
- **容器部署边界**：`docker-compose.example.yml` 只发布前端端口，Nginx 同源代理后端；实际 Compose/配置文件忽略，PG/Redis/Chroma/Skills/日志使用宿主机持久化目录
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
