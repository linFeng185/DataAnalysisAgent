# DataAnalysisAgent — 代码导航指南

## 项目概述

LLM 驱动的数据分析智能体。用自然语言提问，自动完成：轮次状态初始化 → 意图识别 → 表结构检索 → SQL 生成 → 本地安全校验 → 目标库 EXPLAIN → 执行 → 分析 → 图表生成 → 响应组装。

**技术栈**：FastAPI + LangGraph + LangChain + sqlglot + SQLAlchemy + React + TypeScript。

## 目录结构

```
├── src/
│   ├── api/              ① Web 接口层 — 领域路由/纯 ASGI 认证/安全头 + SSE 流式
│   ├── graph/            ② 核心流水线 — LangGraph 16 节点 DAG
│   │   └── nodes/           状态准备、SQL 主链、直接回答与多源节点
│   ├── llm/              ③ LLM 调用层 — Provider 注册表 + 适配器 + Prompt
│   │   └── adapters/         模型适配器
│   ├── datasource/       ④ 数据源管理 — 注册/发现/Schema/凭证加密
│   │   └── providers/        数据源提供者
│   ├── connectors/       ⑤ 数据库连接器 — 自注册方言与统一运行时策略
│   ├── knowledge/        ⑥ 知识库 — 三范围治理 + 标签 + 连接级缓存 + 文档摄取
│   ├── memory/           ⑦ 记忆系统 — 会话持久化 + 上下文裁剪 + 历史
│   ├── tools/            ⑧ 分析工具 — 统计、预测与跨资产分析
│   ├── market/           ⑨ 行情 Provider 与 PostgreSQL 持久化（当前 Tushare/A 股）
│   ├── actions/          ⑩ 受控外部动作（人工确认、幂等、审计）
│   ├── security/         ⑪ 安全模块 — 脱敏 + 限流 + 审计 + 出站地址策略
│   ├── db/               ⑫ 状态库基础设施 — 版本化迁移 + URL 工具
│   ├── mcp_client/       ⑬ MCP 集成 — 客户端管理 + 工具暴露
│   ├── app_context.py        应用级依赖容器 + ASGI 请求绑定 + 资源关闭
│   ├── config.py             配置管理 (pydantic-settings)
│   ├── bootstrap.py          分阶段启动/关闭编排
│   └── skill_manager.py      技能引擎
├── frontend/                 React SPA (Vite + Ant Design + ECharts)
├── skills/                   system 内置 Skills
├── data/skills/              tenant/private 受管 Skills
├── spec/                     技术规格（15 个章节）
├── features/                 功能清单（19 个模块）
├── tests/                    测试
├── migrations/               按编号执行的 PostgreSQL SQL 迁移
└── docs/metrics/             业务指标文档
```

## 核心数据流

```
POST /api/v1/chat {"query": "本月 GMV 排名？", "stream": true}
  │
  ├─ API 安全入口        请求大小/频率限制 → 当前身份数据源授权候选
  ├─ prepare_turn        保留历史和 previous_turn_snapshot，清理当前轮瞬态状态
  ├─ classify_intent     关键词匹配 → 7 种意图 + Skill 激活
  ├─ restore_previous_result  仅 meta 且数据源一致时恢复上轮结构化结果
  ├─ retrieve_schema     SchemaManager 三级回退 → 表结构 + 知识库上下文
  ├─ generate_sql        LLM 生成 SQL（对话历史注入 + 重试上下文）
  ├─ layer3_validate     sqlglot 语法校验 + DDL/DML 安全拦截
  ├─ layer4_explain      复用 Registry 引擎执行方言化 EXPLAIN
  ├─ execute_sql         连接池执行（空 SQL 跳过 / 限流 / 审计）
  ├─ analyze_result      统计计算 + LLM 洞察
  ├─ generate_chart      ECharts 配置生成（桩）
  └─ build_response      响应组装 + 对话历史持久化（dict + messages 双写）
```

## 各模块说明

### ① `src/api/` — Web 接口层

| 文件 | 职责 |
|------|------|
| `routes/__init__.py` | 组合各领域 APIRouter，并保留旧模块导出兼容 |
| `routes/*.py` | chat、datasource、schema、session、mcp、knowledge、skills、management 领域端点 |
| `schemas.py` | Pydantic 请求/响应模型 |
| `streaming.py` | SSE 流式（13 种事件类型，LLM 调用按 stream_id 隔离） |
| `middleware.py` | 异常 → HTTP 状态码映射 |
| `auth.py` | 纯 ASGI JWT/Cookie 认证，身份 ContextVar 覆盖完整流式响应生命周期 |
| `background_tasks.py` | API 后台任务强引用、完成回调和异常记录统一入口 |
| `security_headers.py` | CSP/HSTS/防嵌入/nosniff 纯 ASGI 响应头 |

### ② `src/graph/` — 核心流水线

| 文件 | 职责 |
|------|------|
| `state.py` | `AnalysisState` TypedDict（30+ 字段） |
| `workflow.py` | 从节点目录装配 StateGraph，显式保留条件业务路由 + Checkpointer |
| `node_registry.py` | 节点 handler 与流式进度文案目录 |
| `nodes/prepare_turn.py` | 固化上一轮轻量结果快照，保留对话历史并清空当前轮 SQL/错误/结果/分析状态 |
| `nodes/restore_previous_result.py` | 校验数据源集合后为 meta 追问恢复上轮 SQL、结果样本和统计 |
| `nodes/classify_intent.py` | 意图分类 + Skill 激活 |
| `nodes/retrieve_schema.py` | Schema 检索 + 知识库上下文 |
| `nodes/generate_sql.py` | LLM SQL 生成（对话历史注入） |
| `nodes/layer3_validate.py` | sqlglot AST 只读白名单与危险语句阻断 |
| `nodes/execute_sql.py` | SQL 执行（空 SQL 跳过保护） |
| `nodes/multi_source.py` | 多源 worker 并行执行、维度/指标列契约对齐和来源失败隔离 |
| `nodes/analyze_result.py` | 统计 + LLM 分析 |
| `nodes/build_response.py` | 响应组装、最终 SQL 列表 + 历史记录（dict + messages 双写） |

### ③ `src/llm/` — LLM 调用层

`client.py`（兼容工厂 + local/remote/none 任务路由）+ `provider_registry.py`（OpenAI/Anthropic Provider 注册）+ `adapters/` + `prompts.py`。默认只有 `generate_sql` 可调用远程模型，轻量节点优先 `LOCAL_LLM_*`。

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
| `history_store.py` | PostgreSQL 查询历史 + 内存环形缓冲回退；工作流 await 写入，final_result JSONB 持久化逐轮 SQL、数据、分析、图表和推理 |
| `long_term_store.py` | 长期记忆（ChromaDB + PG 双写） |
| `session_archive.py` | 会话归档 |

### ⑧~⑩ — 工具/安全/MCP

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
| `mcp/client_manager.py` | MCP Client 独立连接栈、自动迁移、system/tenant/private 请求级工具过滤 |
| `mcp/server.py` | MCP Server（暴露 4 个工具） |

### ⑫ `src/db/` — 状态库迁移

`migrations.py` 在应用启动早期按编号扫描 `migrations/*.sql`，使用 PostgreSQL advisory lock
避免多实例并发，按文件事务执行，并在 `schema_migrations` 记录版本、文件名与 checksum。
生产环境迁移失败会阻断启动；开发环境记录错误后允许使用既有回退能力。

## Skills 系统

`src/skill_manager.py` — system/tenant/private 受管目录扫描 + 复合标识 + 请求级身份过滤。匹配同名 Skill 时按 private > tenant > system 选择：
- `data-quality-check` — 空值/重复/异常检测
- `custom-report` — Jinja2 模板报告
- `feature-dev` — 开发流程指南
- `systematic-debugging` — 系统调试协议

## 前端

`frontend/` — React 18 + TypeScript 5 + Ant Design 5 + ECharts。6 页面：对话 / 数据源 / 表结构 / 历史 / Skills / 知识库。

## 快速上手

1. `src/main.py` → `src/bootstrap.py` → `src/api/routes/` — 启动与 API 入口
2. `src/app_context.py` → `src/security/tenant_policy.py` — 应用依赖与租户策略
3. `src/graph/workflow.py` — 流水线组装
4. `src/graph/state.py` — 状态定义
5. `src/graph/nodes/generate_sql.py` — 核心 LLM 调用
6. `spec/README.md` — 技术规格索引
6. `features/README.md` — 功能清单索引

## 关键设计模式

- **分类重试**：SQL/字段/EXPLAIN 语义错误 → generate_sql；连接瞬态错误 → 原 SQL 重试 execute_sql；配置/权限错误终止
- **应用级依赖容器**：每个 FastAPI 应用持有独立 `AppContext`；兼容 `get_*()` 只委托当前 Context，异步资源并发单次初始化并按创建逆序关闭
- **租户策略集中化**：`TenantPolicy` 统一认证门禁、数据源/知识隔离、身份校验和三级作用域写权限，业务模块不直接读取 `settings.multi_tenant`
- **异常回退可见性**：可恢复回退保留原返回契约但必须记录完整堆栈；Provider 等基础设施故障不得伪装成空数据
- **覆盖率门禁**：`coverage run -m pytest -q -m "not live_llm"` 后执行 `coverage report`，branch coverage 最低 67%，生产模块禁止 0% 覆盖
- **多源失败隔离**：每个 worker 独立执行 Schema/SQL/Layer3/EXPLAIN/Execute，不可达来源返回来源级错误
- **跨源列契约**：按全部结果行识别 dimension/metric 角色，角色序列兼容时按位置统一任意数量列；冲突时保留原字段
- **最终 SQL 展示**：多源 worker 返回 execute_sql 方言重写/权限注入后的 SQL，响应通过 sql_statements 按来源展示
- **并行流隔离**：SSE 使用 LangChain run_id 派生 stream_id，前端按调用实例缓冲 thinking/token
- **安全阻断**：layer3 使用 sqlglot AST 只读白名单，阻断 DDL/DML、SELECT INTO 和状态变更函数；表/列解析异常失败关闭
- **向量过滤精确化**：Milvus LIKE 仅缩小候选集，解析 metadata 后再次精确校验，搜索、计数和删除共享同一过滤语义
- **生产密钥门禁**：生产禁止默认数据库凭证、默认凭证主密钥和临时 JWT；Docs、Redoc、OpenAPI 同时关闭
- **API 中间件安全**：纯 ASGI 认证保持 SSE 身份上下文；CORS 默认拒绝跨域；生产 HTTPS 启用 CSP/HSTS
- **出站地址失败关闭**：数据库目标解析为私网、回环或特殊地址时默认阻断；ClickHouse 探针与客户端固定复用已校验 IP
- **授权候选发现**：未选择数据源时仅把当前用户有权访问的候选交给模型，显式越权与权限服务异常均失败关闭
- **版本化迁移**：启动时用 advisory lock + checksum + 单文件事务应用 SQL，生产失败停止启动
- **双重持久化**：`conversation_history`(dict) + `messages`(add_messages) 保证会话跨请求恢复
- **Windows 异步兼容**：`src.main` 为 Uvicorn 显式创建 SelectorEventLoop，保证 psycopg `AsyncPostgresSaver` 可持久化
- **会话逐轮恢复**：每轮 `final_response` 双写 `conversation_history[].final_result` 与 `query_history.final_result` JSONB；前端逐轮消费，禁止复用最后一轮富数据
- **会话恢复回退**：优先读取逐轮 JSONB 和 Checkpointer；旧/缺失状态依次回退 `conversation_history`、输入状态及贫化的 `query_history` 摘要/SQL
- **单一路径工作流**：执行路由统一走条件边，避免并行分支状态丢失
- **三级扩展隔离**：Skill/MCP 使用 system/tenant/private 统一作用域；系统写入仅 super_admin，租户写入仅 tenant_admin/super_admin，个人资源仅本人
- **跨轮结果快照**：当前轮瞬态字段始终清空；只有明确 meta 追问且数据源一致时恢复 previous_turn_snapshot
- **节点级模型降级**：轻量任务本地模型不可用 → 确定性规则；远程任务需显式授权；PG 不可用 → MemorySaver
- **行情先持久化**：MarketDataProvider 请求成功后必须先写入 `market_bars`，写入失败不向分析层返回数据
- **外部动作安全边界**：动作默认拒绝，必须人工确认和幂等键；本项目不实现自动交易
