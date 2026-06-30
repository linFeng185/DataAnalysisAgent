# 14. 设计审查与风险缓解

## 14. 设计审查与风险缓解

本节汇总设计审查中发现的风险点及缓解措施，作为各模块实现时的硬约束。

---

### 14.1 模块边界与循环依赖

| 风险 | 两个模块职责重叠导致循环 import |
|------|-------------------------------|
| 涉及 | `datasource/introspection.py` ↔ `knowledge/schema_manager.py` |
| 缓解 | **已确定边界**: introspection 仅执行原始 SQL 返回 `list[dict]`，不做缓存不做语义加工。schema_manager 负责缓存策略、优先级合并、`SchemaSnapshot` 组装。数据通过 `KnowledgeEntry` 传递。 |
| 涉及 | SPEC 3.8 记忆系统 与 FEATURES.md 第 7 章记忆系统 — 内容分散在两个位置 |
| 缓解 | **两处保留**：SPEC 3.8 描述记忆架构和 LLM 层面的设计决策；FEATURES 第 7 章是施工级清单。各司其职，不合并。 |

---

### 14.2 理想化设计的工程约束

#### 14.2.1 自动发现枚举值 `_auto_discover_enum_values()`

| 风险 | 对大表（10 亿行）执行 `SELECT DISTINCT col LIMIT 50` 可能非常慢 |
|------|--------------------------------------------------------------|
| 缓解 | ① 先查 `_estimate_row_count()`，行数 > 100 万跳过 ② 对 ClickHouse 使用 `uniqCombined` 近似函数 ③ 超时 5 秒自动放弃 ④ 低基数判断阈值从 20 调整为 10（更保守） |

#### 14.2.2 上下文摘要 `_summarize_turns()`

| 风险 | 每次 LLM 调用都对温数据做 `cheap_llm` 摘要，增加延迟和成本 |
|------|----------------------------------------------------------|
| 缓解 | ① Phase 1 使用规则摘要（拼接 turn 首句），不调 LLM ② Phase 2 改为后台异步预计算：会话归档时一并生成摘要，写入 `ConversationTurn.summary` 字段 ③ `build_llm_context` 直接读取已算好的摘要 |

#### 14.2.3 EXPLAIN 空跑成本

| 风险 | Snowflake 等云数仓执行 EXPLAIN 可能产生计算费用或消耗 warehouse 资源 |
|------|-------------------------------------------------------------------|
| 缓解 | ① 增加配置项 `EXPLAIN_SKIP_DIALECTS`（默认 `{"snowflake"}`）② 用户可在 `config/datasources.yaml` 中按数据源覆盖 ③ 跳过的方言仅依赖 Layer 3 sqlglot 校验 |

#### 14.2.4 `compute_statistics()` 内存安全

| 风险 | 基于完整结果集（可能百万行）执行 pandas 计算导致 OOM |
|------|---------------------------------------------------|
| 缓解 | ① 统计计算输入行数上限 `MAX_STATS_ROWS`（默认 50 万行）② 超限时在 DB 层用 `APPROX_QUANTILES` / `approx_count_distinct` 做近似统计 ③ `query_result_full_count` 告知 LLM 总行数 |

---

### 14.3 异常与降级场景

#### 14.3.1 DB 权限不足

| 风险 | 查询 `INFORMATION_SCHEMA` 权限不足时仅返回降级信息，用户不知道发生了什么 |
|------|---------------------------------------------------------------------|
| 缓解 | ① 权限错误时 `SchemaManager` 在返回的 `SchemaSnapshot` 中插入一条特殊的 `KnowledgeEntry`，`source=SYSTEM_WARNING`，内容说明缺少哪些权限及如何授权 ② LLM 在分析结果中向用户展示 ③ 同时写入 structlog 日志并触发 Prometheus alert |

#### 14.3.2 MCP Server 断线不可恢复

| 风险 | 指数退避重连全部失败后，依赖该 MCP 的查询永久挂起 |
|------|--------------------------------------------------|
| 缓解 | ① 最大重试 5 次（总窗口 ~2 分钟），之后标记该 MCP Server 为 `degraded` ② `degraded` 状态的 MCP 工具从 `get_all_tools()` 中移除，查询走无该工具路径 ③ 健康检查恢复后自动重新启用 ④ 管理员可通过 API 手动 `POST /api/v1/mcp/{name}/reset` 恢复 |

#### 14.3.3 长时间无文档

| 风险 | 用户从不提供 Markdown 文档，全部依赖 `source=auto` 自动拉取，字段语义质量差 |
|------|-------------------------------------------------------------------------|
| 缓解 | ① 当 `source=auto` 占比 > 80% 且有查询活动时，LLM 在分析结果中主动提示用户补充文档 ② 频率限制：每 24 小时最多提示 1 次 |

---

### 14.4 数据一致性与并发

#### 14.4.1 长期记忆双写 `_upsert()`

| 风险 | 先写 ChromaDB 成功但 PostgreSQL 失败 → 向量检索能查到但结构化字段丢失 |
|------|--------------------------------------------------------------------|
| 缓解 | ① 严格执行顺序: **先 PG，后 ChromaDB**。PG 是权威数据源（结构化查询依赖它），ChromaDB 是检索加速层 ② PG 写入失败直接抛异常不回退（向量库无脏数据） ③ PG 成功但 ChromaDB 失败时：记录到 `pending_vector_sync` 表，后台补偿任务每 5 分钟重试 ④ `search()` 始终以 PG 结果为准 |

#### 14.4.2 缓存刷新并发

| 风险 | 多个请求同时发现缓存过期 → 发起多个 DB 内省任务 → 对源数据库造成压力 |
|------|------------------------------------------------------------------|
| 缓解 | ① 使用 Redis `SETNX` 分布式锁（key=`refresh_lock:{ds}:{table}`，TTL=60s）② 未获取锁的请求返回旧缓存（允许过期数据） ③ 无 Redis 时退化为进程内 `asyncio.Lock`（单进程安全） |

---

### 14.5 性能与资源规划

#### 14.5.1 响应时间目标与资源约束

| 指标 | 目标 | 资源约束 |
|------|------|---------|
| 简单查询端到端 | ≤ 3 秒 | 不含 LLM 推理时间，仅计算 SQL 执行 + 结果处理 |
| 复杂分析端到端 | ≤ 30 秒 | 含 LLM 分析 + 图表生成 |
| 连接池大小 | — | ClickHouse/MySQL/PG 每数据源默认 `pool_size=5, max_overflow=10`，可在 `DataSourceConfig.extra_params` 覆盖 |
| ChromaDB 容量 | — | 设计容量 10 万条 KnowledgeEntry，单条 embedding 768d。超过后启用 LRU 淘汰（优先删除 `source=auto` 且 `access_count=0` 的条目） |
| LLM 并发 | — | 默认 5 个并发调用（通过 `asyncio.Semaphore` 控制），可在 Settings 中覆盖 |

#### 14.5.2 分页与大数据量

| 场景 | 策略 |
|------|------|
| API 列表（数据源/表/Schema） | 支持 `?page=1&page_size=20&search=xxx` 分页 |
| SQL 查询结果 | 200 行截断给 LLM，全量通过 `query_result_full_count` 告知行数 |
| 统计计算 | 输入 ≤ 50 万行，超限走 DB 近似统计（见 14.2.4） |

---

### 14.6 API 设计补全

#### 14.6.1 新增路由

| 路由 | 说明 | 优先级 |
|------|------|--------|
| `PUT /api/v1/schema/tables/{table}/columns/{column}/comment` | 手动标注字段中文说明，直接写入 ChromaDB | P1 |
| `GET /api/v1/schema/tables?datasource=x&page=1&page_size=20&search=y` | 表列表分页 + 搜索 | P1 |
| `POST /api/v1/mcp/{name}/reset` | 手动重置 degraded 状态的 MCP Server | P1 |
| `GET /api/v1/metrics` | 查询已注册的指标口径列表 | P2 |

#### 14.6.2 现有路由补全分页参数

`GET /api/v1/schema/tables`、`GET /api/v1/schema/tables/{table_name}`、`GET /api/v1/history`、`GET /api/v1/datasources` 均增加 `?page&page_size` 分页参数。

---

### 14.7 测试策略细化

#### 14.7.1 Mock 方案

| 场景 | Mock 方式 |
|------|----------|
| Mock LLM 输出固定 SQL | 使用 `FakeListChatModel`（langchain_core 内置），按顺序返回预设 `AIMessage` |
| Mock DB 返回特定结果集 | 使用 SQLite 内存数据库（`:memory:`）作为 Connector 的测试后端，建表 + 插数据 → 验证 SQL 正确性 |
| Mock MCP Server | 创建一个 `StaticMCPTestServer`，注册固定 tools，不依赖外部进程 |
| Mock ChromaDB | 使用 `chromadb.Client()` 的 `EphemeralClient` 模式 |
| LangGraph 条件边测试 | 构造特定 `AnalysisState` 直接调用 `after_layer3(state)` / `should_retry(state)`，验证返回值 |

#### 14.7.2 测试分层

```
Layer 1: 纯函数单元测试 (validate_with_sqlglot, compute_statistics...)
         → SQLite / EphemeralClient，无需外部依赖

Layer 2: Node 集成测试 (单个 Node 的 prompt → chain → 输出)
         → FakeListChatModel + SQLite

Layer 3: Graph 集成测试 (完整 StateGraph + 条件边)
         → FakeListChatModel + SQLite + assert 最终 state

Layer 4: API 集成测试 (httpx AsyncClient 完整请求)
         → 需要真实 DB 或 Docker Compose 的测试环境
```

---

### 14.8 版本管理

#### 14.8.1 Prompt 版本与 A/B 测试

| 能力 | 实现方式 |
|------|---------|
| Prompt 版本号 | `VERSION = "1.2.0"` 常量，LangSmith 自动记录每次调用的 Prompt 版本 |
| A/B 测试 | 按 `user_id % 2` 分流：`prompts.py` 中 `get_prompt(name, variant)` 根据分流返回 A 或 B 版本 |
| 回滚 | LangSmith UI 对比版本质量 → 修改 `DEFAULT_PROMPT_VERSIONS` 字典切换到指定版本 |
| 自动升级 | 不自动升级。新版本手动标记为 `active`，旧版本保留 30 天 |

#### 14.8.2 Skill 版本依赖

| 风险 | Skill A 依赖 `pandas>=2.0`，Skill B 依赖 `pandas<2.0` |
|------|--------------------------------------------------------|
| 缓解 | ① Phase 1-2: 依赖声明仅做检查提示，不强制 ② Phase 3: 引入 `pip` resolver 在 Skill 激活时检测冲突，冲突时禁止同时激活 ③ 社区 Skill 要求声明 `depends_on.python_packages` 并给出版本范围 |

---

### 14.9 安全增强

| # | 补充项 | 说明 | 优先级 |
|---|--------|------|--------|
| 14.9.1 | 查询白名单模式 | Phase 3 增加按表/字段粒度的访问白名单（`config/access_policy.yaml`） | P2 |
| 14.9.2 | 查询结果水印 | 分析结果中追加"此数据由 AI 生成，请以原始数据为准"声明 | P2 |
| 14.9.3 | LLM 输出二次校验 | `generate_sql` 输出的 SQL 中引用的表名/字段名必须全在 `state["relevant_tables"]` 中存在 | P1 |

---

### 14.10 变更摘要

| 章节 | 变更类型 | 描述 |
|------|---------|------|
| 3.2 | 边界明确 | introspection ↔ schema_manager 职责划分 |
| 3.2.5 | 安全约束 | `_auto_discover_enum_values` 增加行数阈值和超时 |
| 3.2.6 | 并发控制 | CacheRefresher 增加 Redis 分布式锁 |
| 3.4.2 | 可配置 | EXPLAIN 增加 `EXPLAIN_SKIP_DIALECTS` 跳过配置 |
| 3.8.5 | 性能 | `_summarize_turns` Phase 1 用规则替代 LLM，Phase 2 异步预计算 |
| 4.7 | 内存安全 | compute_statistics 增加 `MAX_STATS_ROWS` 上限 |
| 14.x | 新增 | 异常降级策略、双写事务顺序、权限告警、MCP 降级、Mock 方案、API 补全、版本管理 |
