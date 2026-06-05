# 数据智能体 — 功能开发清单

> 状态: 待开发 / 开发完成 / 单测完成 / 集成测试完成
> 优先级: P0 (Phase 1) / P1 (Phase 2) / P2 (Phase 3) / P3 (Phase 4)
> DoD: 见 CLAUDE.md § Definition of Done
> 生成自: SPEC.md (2026-06-05)
> 总功能点: 365  | 去重后: ~338

---

## 1. 项目基础设施 `[P0:10 P1:4]`

### 1.1 项目骨架

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 1.1.1 | Poetry 项目初始化 | `pyproject.toml` | 创建 Poetry 管理的 Python 项目，定义依赖 | 单测完成 | P0 |
| 1.1.2 | FastAPI 应用入口 | `src/main.py` | FastAPI 实例化、生命周期管理、路由挂载 | 单测完成 | P0 |
| 1.1.3 | 配置管理 | `src/config.py` | 基于 pydantic-settings 的 Settings 类，从 .env / 环境变量 / YAML 加载配置 | 单测完成 | P0 |
| 1.1.4 | Docker Compose | `docker-compose.yml` | 开发环境容器编排（PostgreSQL 17 + Redis 7 + App） | 单测完成 | P0 |
| 1.1.5 | requirements.txt | `requirements.txt` | 生产环境 pip 依赖固定版本 | 单测完成 | P0 |
| 1.1.6 | 日志配置 | `src/logging_config.py` | structlog 结构化日志，支持 JSON/Console 双格式，区分开发/生产 | 单测完成 | P0 |
| 1.1.7 | 异常体系 | `src/exceptions.py` | DataSourceNotFoundError、SQLValidationError、ExecutionError、RateLimitError 等自定义异常 | 单测完成 | P0 |

### 1.2 配置体系

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 1.2.1 | Settings 类 | `src/config.py` | LLM API Key / DB 连接串 / Redis URL / ChromaDB 路径 / 限流参数 / 日志级别 | 单测完成 | P0 |
| 1.2.2 | .env 模板 | `.env.example` | 所有可配置环境变量的模板文件 | 单测完成 | P0 |
| 1.2.3 | MCP Server 注册表 | `config/mcp_servers.yaml` | 声明所有外部 MCP Server（transport / command / args / url） | 待开发 | P0 |
| 1.2.4 | 数据源配置文件 | `config/datasources.yaml` | 外挂模式的数据源声明（dialect / host / port / database / 凭证引用） | 待开发 | P0 |

### 1.3 异常与错误处理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 1.3.1 | DataSourceNotFoundError | `src/exceptions.py` | 数据源未找到异常 | 单测完成 | P0 |
| 1.3.2 | SQLValidationError | `src/exceptions.py` | SQL 校验失败异常，携带 errors/warnings 列表 | 单测完成 | P0 |
| 1.3.3 | SQLSecurityError | `src/exceptions.py` | SQL 安全拦截异常（含拦截原因和违规操作） | 单测完成 | P0 |
| 1.3.4 | ExecutionError | `src/exceptions.py` | SQL 执行失败异常，携带原始错误信息和 retry_count | 单测完成 | P0 |
| 1.3.5 | RateLimitError | `src/exceptions.py` | 请求频率超限异常 | 单测完成 | P0 |
| 1.3.6 | KnowledgeNotFoundError | `src/exceptions.py` | 知识库未找到相关知识异常 | 单测完成 | P0 |
| 1.3.7 | 全局异常处理中间件 | `src/api/middleware.py` | FastAPI exception_handler，统一错误响应格式 | 待开发 | P0 |

---

## 2. 数据源管理模块 (datasource/) `[P0:20 P1:12 P2:3 P3:1]`

### 2.1 配置对象与注册

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.1.1 | DataSourceConfig 定义 | `src/datasource/config.py` | dataclass: name/dialect/mode/host/port/database/username/password/engine/schema/description/tags/extra_params | 待开发 |
| 2.1.2 | DataSourceRegistry | `src/datasource/registry.py` | register_provider() / resolve() / list_all() / _create_engine() | 待开发 |
| 2.1.3 | DataSourceProvider 抽象基类 | `src/datasource/providers/base.py` | lookup() / extract_schema() / _test_connection() 抽象方法 | 待开发 |
| 2.1.4 | DataSourceConfigStore | `src/datasource/config_store.py` | save() / delete() / list_all() — 外挂模式配置持久化到 PostgreSQL | 待开发 |
| 2.1.5 | DataSourceCreateRequest | `src/api/schemas.py` | Pydantic model: 外挂模式注册数据源的请求体 | 待开发 |

### 2.2 内置模式 Provider (Embedded)

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.2.1 | EmbeddedDataSourceProvider 类 | `src/datasource/providers/embedded.py` | 实现 DataSourceProvider 接口 | 待开发 |
| 2.2.2 | auto_discover() | 同上 | 自动发现项目中的数据库连接 | 待开发 |
| 2.2.3 | _is_django_project() | 同上 | 检测当前环境是否为 Django 项目 | 待开发 |
| 2.2.4 | _from_django_config() | 同上 | 从 Django settings.DATABASES 解析数据源配置 | 待开发 |
| 2.2.5 | _has_sqlalchemy_engine() | 同上 | 检测是否存在 SQLAlchemy engine 实例 | 待开发 |
| 2.2.6 | _from_sqlalchemy_engine() | 同上 | 从 SQLAlchemy engine 提取连接信息 | 待开发 |
| 2.2.7 | _from_env_vars() | 同上 | 从环境变量 (DB_HOST/DB_PORT 等) 解析数据源配置 | 待开发 |
| 2.2.8 | _find_orm_models() | 同上 | 扫描项目中的 SQLAlchemy declarative_base / Django Model 类 | 待开发 |
| 2.2.9 | _extract_model_description() | 同上 | 从 Model.__doc__ / Model._meta.verbose_name 提取中文描述 | 待开发 |
| 2.2.10 | extract_schema() — ORM 优先 | 同上 | ① ORM Model ② migration 注释 ③ DB 内省，三级回退提取 Schema | 待开发 |

### 2.3 外挂模式 Provider (External)

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.3.1 | ExternalDataSourceProvider 类 | `src/datasource/providers/external.py` | 实现 DataSourceProvider 接口 | 待开发 |
| 2.3.2 | register() | 同上 | 测试连接 → 加密凭证 → 持久化 → 异步预采集 Schema | 待开发 |
| 2.3.3 | unregister() | 同上 | 移除数据源，关闭连接池，清理缓存 | 待开发 |
| 2.3.4 | _test_connection() | 同上 | 发送 SELECT 1 / EXPLAIN 确认连接可用 | 待开发 |
| 2.3.5 | extract_schema() — 纯内省 | 同上 | INFORMATION_SCHEMA 查询 + 手工标注文件补充 | 待开发 |
| 2.3.6 | YAML 加载器 | 同上 | load_yaml_datasources() — 解析 config/datasources.yaml | 待开发 |
| 2.3.7 | API 动态注册路由 | `src/api/routes.py` | POST /api/v1/datasources — 运行时注册数据源 | 待开发 |
| 2.3.8 | API 删除路由 | `src/api/routes.py` | DELETE /api/v1/datasources/{name} — 运行时移除数据源 | 待开发 |
| 2.3.9 | API 列表路由 | `src/api/routes.py` | GET /api/v1/datasources — 列出所有数据源及其状态 | 待开发 |

### 2.4 凭证管理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.4.1 | CredentialManager | `src/datasource/credential_manager.py` | encrypt() / decrypt() — AES-256 加密，密钥来自环境变量或 KMS | 待开发 |
| 2.4.2 | 环境变量凭证引用 | 同上 | 解析 `${VAR_NAME}` 占位符，从 os.environ 获取真实值 | 待开发 |
| 2.4.3 | KMS 集成 (远期) | 同上 | 对接 Vault / AWS KMS / Azure Key Vault | 待开发 |

### 2.5 DB 内省

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.5.1 | _query_columns() — ClickHouse | `src/datasource/introspection.py` | `SELECT name, type, comment FROM system.columns` | 待开发 |
| 2.5.2 | _query_columns() — MySQL | 同上 | `SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS` | 待开发 |
| 2.5.3 | _query_columns() — PostgreSQL | 同上 | `SELECT column_name, data_type, col_description(...) FROM INFORMATION_SCHEMA.COLUMNS` | 待开发 |
| 2.5.4 | _query_foreign_keys() — MySQL | 同上 | INFORMATION_SCHEMA.TABLE_CONSTRAINTS + KEY_COLUMN_USAGE 查询外键 | 待开发 |
| 2.5.5 | _query_foreign_keys() — PostgreSQL | 同上 | pg_catalog.pg_constraint 查询外键 | 待开发 |
| 2.5.6 | _query_foreign_keys() — ClickHouse | 同上 | ClickHouse 不支持外键，返回空列表 | 待开发 |
| 2.5.7 | _estimate_row_count() — ClickHouse | 同上 | `SELECT COUNT(*)` 或 `system.parts` 估算 | 待开发 |
| 2.5.8 | _estimate_row_count() — MySQL | 同上 | `SELECT TABLE_ROWS FROM INFORMATION_SCHEMA.TABLES` | 待开发 |
| 2.5.9 | _estimate_row_count() — PostgreSQL | 同上 | `SELECT reltuples FROM pg_class` | 待开发 |
| 2.5.10 | _query_metadata() 权限告警 | 同上 | INFORMATION_SCHEMA 权限不足时写入 SYSTEM_WARNING 类型的 KnowledgeEntry | 待开发 | P1 |

### 2.6 Schema 数据结构

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.6.1 | SchemaSnapshot 定义 | `src/datasource/schema_snapshot.py` | dataclass: tables / field_semantics / business_rules / sql_templates | 待开发 |
| 2.6.2 | SchemaSnapshot.to_prompt_text() | 同上 | 格式化为 LLM Prompt 可用的 Markdown 表格文本 | 待开发 |
| 2.6.3 | SchemaSnapshot.merge() | 同上 | 合并多个 SchemaSnapshot（ORM + 内省结果） | 待开发 |
| 2.6.4 | TableSchema 定义 | 同上 | dataclass: name / description / columns / relations / row_count_estimate / partition_key / tags | 待开发 |
| 2.6.5 | ColumnInfo 定义 | 同上 | dataclass: name / type / comment / is_nullable / is_primary_key / enum_values | 待开发 |
| 2.6.6 | TableRelation 定义 | 同上 | dataclass: target_table / join_key / relation_type | 待开发 |

---

## 3. 数据库连接器 (connectors/) `[P0:8 P1:2 P2:4]`

### 3.1 连接器基类

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.1.1 | ConnectorBase 抽象类 | `src/connectors/base.py` | execute() / explain() / health_check() / close() 抽象方法 | 待开发 |
| 3.1.2 | 连接池工厂 | 同上 | create_async_engine() — 统一创建 SQLAlchemy AsyncEngine，配置 pool_size / max_overflow / timeout | 待开发 |
| 3.1.3 | 查询超时控制 | 同上 | 根据 dialect 设置 statement_timeout / max_execution_time | 待开发 |
| 3.1.4 | 结果格式化 | 同上 | rows_to_dict_list() — 将数据库行转为 list[dict] | 待开发 |

### 3.2 ClickHouse 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.2.1 | ClickHouseConnector | `src/connectors/clickhouse.py` | 继承 ConnectorBase，使用 clickhouse-connect 异步驱动 | 待开发 |
| 3.2.2 | execute() | 同上 | 执行 SELECT 查询，返回结果列表 | 待开发 |
| 3.2.3 | explain() | 同上 | `EXPLAIN SYNTAX {sql}` — 语法空跑 | 待开发 |
| 3.2.4 | health_check() | 同上 | `SELECT 1` 连通性检查 | 待开发 |
| 3.2.5 | 分区键获取 | 同上 | 查询 system.parts 获取表的分区信息 | 待开发 |

### 3.3 MySQL 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.3.1 | MySQLConnector | `src/connectors/mysql.py` | 继承 ConnectorBase，使用 aiomysql 驱动 | 待开发 |
| 3.3.2 | execute() | 同上 | 执行 SELECT 查询 | 待开发 |
| 3.3.3 | explain() | 同上 | `EXPLAIN FORMAT=TREE {sql}` | 待开发 |
| 3.3.4 | health_check() | 同上 | `SELECT 1` | 待开发 |

### 3.4 PostgreSQL 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.4.1 | PostgreSQLConnector | `src/connectors/postgres.py` | 继承 ConnectorBase，使用 asyncpg 驱动 | 待开发 |
| 3.4.2 | execute() | 同上 | 执行 SELECT 查询 | 待开发 |
| 3.4.3 | explain() | 同上 | `EXPLAIN (ANALYZE false) {sql}` | 待开发 |
| 3.4.4 | health_check() | 同上 | `SELECT 1` | 待开发 |

---

## 4. LangGraph 编排引擎 (graph/) `[P0:28 P1:16 P2:3 P3:1]`

### 4.1 状态定义与工作流组装

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.1.1 | AnalysisState TypedDict | `src/graph/state.py` | 定义所有在图中流转的字段及类型 (17 个字段) | 待开发 |
| 4.1.2 | StateGraph 组装 | `src/graph/workflow.py` | 注册 10 个 Node + 定义边 + 条件路由 + compile | 待开发 |
| 4.1.3 | after_layer3() 条件路由 | 同上 | sqlglot 校验后的路由: generate_sql / layer4_explain / build_response | 待开发 |
| 4.1.4 | after_layer4() 条件路由 | 同上 | EXPLAIN 空跑后的路由: generate_sql / execute_sql / build_response | 待开发 |
| 4.1.5 | should_retry() 条件路由 | 同上 | 执行失败后的路由: generate_sql(重试) / build_response(放弃) | 待开发 |
| 4.1.6 | route_by_intent() 条件路由 | 同上 | 意图为 file_analysis 时路由到 mcp_agent Node | 待开发 |
| 4.1.7 | MCP Agent Node 扩展 | 同上 | 使用 create_react_agent 为文件分析场景创建动态工具调用 Node | 待开发 |

### 4.2 classify_intent Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.2.1 | classify_intent_node() | `src/graph/nodes/classify_intent.py` | LLM 判断意图类型: query / aggregation / attribution / trend / metadata / chat / file_analysis | 待开发 |
| 4.2.2 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别的 ChatPromptTemplate | 待开发 |
| 4.2.3 | Skill 匹配触发 | 同上 | 调用 skill_manager.match_skills() 激活相关 Skill | 待开发 |
| 4.2.4 | 输出: intent / activated_skills / skill_prompt_override / skill_tools | 同上 | 将激活的 Skill 信息写入 state | 待开发 |

### 4.3 retrieve_schema Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.3.1 | retrieve_schema_node() | `src/graph/nodes/retrieve_schema.py` | 关键词提取实体 → 向量检索表结构 → 检索业务文档 → 组装 SchemaSnapshot | 待开发 |
| 4.3.2 | 关键词提取 | 同上 | 从 user_query 中提取表名/字段名/指标名关键实体 | 待开发 |
| 4.3.3 | 向量检索表结构 | 同上 | ChromaDB 语义检索 + 关键词精确匹配，表级索引返回 Top-5 相关表 | 待开发 |
| 4.3.4 | 向量检索字段语义 | 同上 | ChromaDB 字段级索引检索，返回 Top-10 相关字段说明 | 待开发 |
| 4.3.5 | 检索业务规则 | 同上 | BusinessRuleStore.search_business_rules() | 待开发 |
| 4.3.6 | 检索历史 SQL 模板 | 同上 | LongTermMemoryStore.search(memory_type=SQL_TEMPLATE) | 待开发 |
| 4.3.7 | Few-shot 示例组装 | 同上 | 将检索到的 (question, schema, dialect, sql) 四元组注入 Prompt | 待开发 |
| 4.3.8 | 输出: resolved_schema / relevant_tables / few_shot_examples / business_rules_text / long_term_memories_text | 同上 | | 待开发 |

### 4.4 generate_sql Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.4.1 | generate_sql_node() | `src/graph/nodes/generate_sql.py` | LLM 生成 SQL，支持首次生成和错误回注重试 | 待开发 |
| 4.4.2 | SQL_GENERATION_SYSTEM_PROMPT | `src/llm/prompts.py` | SQL 生成的系统 Prompt 模板 | 待开发 |
| 4.4.3 | 方言 Prompt 注入 | 同上 | 根据 state["dialect"] 注入对应的函数速查表 | 待开发 |
| 4.4.4 | Few-shot 注入 | 同上 | 将 state["few_shot_examples"] 注入 user prompt | 待开发 |
| 4.4.5 | 业务规则注入 | 同上 | 将 state["business_rules_text"] 注入 prompt | 待开发 |
| 4.4.6 | 错误回注处理 | 同上 | 重试时拼接 validation_errors + previous_sql 到 Prompt | 待开发 |
| 4.4.7 | 最后一次尝试强化提示 | 同上 | retry_count >= 2 时追加 "请仔细核对字段名和函数名" | 待开发 |
| 4.4.8 | JsonOutputParser + SQLOutput | 同上 | 标准化输出: {"sql": "...", "explanation": "..."} | 待开发 |
| 4.4.9 | 上下文裁剪 | 同上 | 调用 build_llm_context(node_name="generate_sql") | 待开发 |
| 4.4.10 | 输出: generated_sql / retry_count | 同上 | | 待开发 |

### 4.5 layer3_validate Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.5.1 | layer3_validate_node() | `src/graph/nodes/layer3_validate.py` | 调用 validate_with_sqlglot() + 安全拦截 | 待开发 |
| 4.5.2 | SQL 安全拦截 (DDL/DML 黑名单) | 同上 | 正则匹配拦截 INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/GRANT/REVOKE/sleep/benchmark | 待开发 |
| 4.5.3 | sqlglot 语法解析校验 | 同上 | sqlglot.parse(sql, dialect=dialect) — 拦截语法错误 | 待开发 |
| 4.5.4 | sqlglot 函数白名单校验 | 同上 | 遍历 AST 检查每个函数是否在目标方言中存在 | 待开发 |
| 4.5.5 | sqlglot 方言转译 | 同上 | sqlglot.transpile(sql, read="mysql", write=dialect) | 待开发 |
| 4.5.6 | 函数修正建议 | 同上 | _suggest_correct_function() — 维护 ClickHouse/PostgreSQL 函数映射表 | 待开发 |
| 4.5.7 | 输出: sql_valid / validation_errors / validation_warnings / transpiled_sql | 同上 | | 待开发 |

### 4.6 layer4_explain Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.6.1 | layer4_explain_node() | `src/graph/nodes/layer4_explain.py` | 执行 EXPLAIN 空跑校验 | 待开发 |
| 4.6.2 | EXPLAIN_TEMPLATES 字典 | 同上 | ClickHouse/MySQL/PostgreSQL/Presto/Hive 各自的 EXPLAIN 语法 | 待开发 |
| 4.6.3 | explain_check() | 同上 | 在目标 DB 执行 EXPLAIN，捕获语义错误并提取友好信息 | 待开发 |
| 4.6.4 | 输出: sql_valid / validation_errors | 同上 | | 待开发 |

### 4.7 execute_sql Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.7.1 | execute_sql_node() | `src/graph/nodes/execute_sql.py` | 通过 DataSourceRegistry 获取 engine → 执行 SQL | 待开发 |
| 4.7.2 | 数据结果截断 (200行) | 同上 | LLM 分析只需前 200 行，全量结果保留给统计计算 | 待开发 |
| 4.7.3 | compute_statistics() | 同上 | 自动计算 pandas 统计摘要: 行数 / 数值列均值中位数 / 空值率 / 唯一值数 | 待开发 |
| 4.7.4 | 超时控制 | 同上 | 设置 statement_timeout 和执行超时，超时写 execution_error | 待开发 |
| 4.7.5 | 限流检查 | 同上 | 检查用户每小时查询次数，超限返回 RateLimitError | 待开发 |
| 4.7.6 | 输出: query_result_sample / query_result_full_count / query_result_statistics / execution_error | 同上 | | 待开发 |

### 4.8 analyze_result Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.8.1 | analyze_result_node() | `src/graph/nodes/analyze_result.py` | LLM 解读查询结果 + pandas 统计补充 | 待开发 |
| 4.8.2 | DATA_ANALYSIS_PROMPT | `src/llm/prompts.py` | 数据分析的系统 Prompt: 数据摘要 / 关键发现 / 推荐图表 / 追问方向 | 待开发 |
| 4.8.3 | 描述性统计 | 同上 | 对所有数值列计算 均值/中位数/标准差/分位数 | 待开发 |
| 4.8.4 | 趋势分析 | 同上 | 时间序列数据: 同比/环比/移动平均 | 待开发 |
| 4.8.5 | 归因分析 | 同上 | 用户指定 "为什么" 时: 维度下钻寻找变化根因 | 待开发 |
| 4.8.6 | 异常检测 | 同上 | Z-Score / IQR 方法识别离群值 | 待开发 |
| 4.8.7 | 占比分析 | 同上 | 分类维度 + 数值指标: 帕累托分析 / 集中度 | 待开发 |
| 4.8.8 | 上下文裁剪 | 同上 | 调用 build_llm_context(node_name="analyze_result") | 待开发 |
| 4.8.9 | 输出: analysis_result (summary + insights + recommended_chart_type + follow_up_questions) | 同上 | | 待开发 |

### 4.9 generate_chart Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.9.1 | generate_chart_node() | `src/graph/nodes/generate_chart.py` | LLM 根据列类型自动选择图表类型 + 生成 ECharts config | 待开发 |
| 4.9.2 | 智能选图逻辑 | 同上 | 时间列→折线图 / 分类列+数值列→柱状图 / 占比→饼图 / 双数值列→散点图 / 交叉维度→热力图 | 待开发 |
| 4.9.3 | ECharts option 生成 | 同上 | 生成 JSON 格式的 ECharts 配置 | 待开发 |
| 4.9.4 | CHART_RECOMMEND_PROMPT | `src/llm/prompts.py` | 图表推荐的 ChatPromptTemplate | 待开发 |
| 4.9.5 | 输出: chart_config (type + echarts_option) | 同上 | | 待开发 |

### 4.10 build_response Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.10.1 | build_response_node() | `src/graph/nodes/build_response.py` | 组装最终响应: user_query + sql + data + analysis + chart | 待开发 |
| 4.10.2 | 正常响应组装 | 同上 | 7 个流水线 Node 全部成功 → 完整响应 JSON | 待开发 |
| 4.10.3 | 错误响应组装 | 同上 | 任意 Node 失败 → 返回 error_code + error_message + suggestion | 待开发 |
| 4.10.4 | SQL 模板归档 | 同上 | 成功的 SQL 自动写入 LongTermMemoryStore.save_sql_template() | 待开发 |
| 4.10.5 | 输出: final_response (完整 JSON，包含所有字段) | 同上 | | 待开发 |

---

## 5. 工具层 (tools/) `[P0:11 P1:2]`

### 5.1 内置工具

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 5.1.1 | SchemaExplorerTool | `src/tools/schema_explorer.py` | 继承 BaseTool，封装 SchemaManager.get_or_fetch_schema() | 待开发 |
| 5.1.2 | SQLGeneratorTool | `src/tools/sql_generator.py` | 继承 BaseTool，封装 SQL 生成逻辑 | 待开发 |
| 5.1.3 | SQLglotValidatorTool | `src/tools/sqlglot_validator.py` | 继承 BaseTool，封装 validate_with_sqlglot() | 待开发 |
| 5.1.4 | DBExecutorTool | `src/tools/db_executor.py` | 继承 BaseTool，封装 SQL 执行逻辑 | 待开发 |
| 5.1.5 | DBExplainTool | `src/tools/db_explain.py` | 继承 BaseTool，封装 EXPLAIN 空跑逻辑 | 待开发 |
| 5.1.6 | DataAnalyzerTool | `src/tools/analyzer.py` | 继承 BaseTool，封装数据分析逻辑 | 待开发 |
| 5.1.7 | ChartGeneratorTool | `src/tools/chart_generator.py` | 继承 BaseTool，封装图表生成逻辑 | 待开发 |

### 5.2 sqlglot 校验工具

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 5.2.1 | validate_with_sqlglot() | `src/tools/sqlglot_validator.py` | 核心校验函数: 语法解析 + 函数白名单 + 方言转译 | 待开发 |
| 5.2.2 | SUPPORTED_DIALECTS 常量 | 同上 | 20+ 种 sqlglot 支持的方言集合 | 待开发 |
| 5.2.3 | _get_dialect_functions() | 同上 | 获取指定方言的内置函数白名单 | 待开发 |
| 5.2.4 | _is_universal_func() | 同上 | 跨数据库通用函数集合 (COUNT/SUM/AVG/COALESCE...) | 待开发 |
| 5.2.5 | _suggest_correct_function() | 同上 | ClickHouse/PostgreSQL 函数映射建议表 | 待开发 |

---

## 6. 知识库管理 (knowledge/) `[P0:14 P1:10 P2:4 P3:1]`

### 6.1 Schema 缓存管理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.1.1 | SchemaManager 类 | `src/knowledge/schema_manager.py` | 三级回退: ChromaDB 缓存 → 文档仓库 → DB 自动拉取 | 待开发 |
| 6.1.2 | get_or_fetch_schema() | 同上 | Schema 获取的主入口，包装完整三级回退逻辑 | 待开发 |
| 6.1.3 | _query_cache() | 同上 | 查 ChromaDB 缓存，检查 TTL 是否过期 | 待开发 |
| 6.1.4 | _find_uncached() | 同上 | 找出哪些表尚未缓存或已过期 | 待开发 |
| 6.1.5 | _load_from_docs() | 同上 | 从 docs/metrics/ 目录加载 Markdown 文档 | 待开发 |
| 6.1.6 | _introspect_from_db() | 同上 | DB 系统表自动拉取，生成表级 + 字段级双粒度索引 | 待开发 |
| 6.1.7 | _upsert_to_cache() | 同上 | KnowledgeEntry 写入 ChromaDB | 待开发 |
| 6.1.8 | _build_snapshot() | 同上 | 将所有 KnowledgeEntry 组装为 SchemaSnapshot | 待开发 |
| 6.1.9 | _format_table_summary() | 同上 | 格式化为表级描述文本 | 待开发 |
| 6.1.10 | _format_column_detail() | 同上 | 格式化为字段级描述文本（含 source=auto 标记） | 待开发 |
| 6.1.11 | _execute_metadata_query() | 同上 | 执行元数据查询（带超时和错误处理） | 待开发 |

### 6.2 知识条目

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.2.1 | KnowledgeSource Enum | `src/knowledge/schema_manager.py` | MANUAL_DOC / ORM_MODEL / DB_COMMENT / AUTO_INTROSPECT / USER_CORRECTION | 待开发 |
| 6.2.2 | KnowledgeEntry dataclass | 同上 | id / content / source / table_name / column_name / category / tags / created_at / ttl | 待开发 |
| 6.2.3 | 知识优先级判断 | 同上 | MANUAL_DOC > USER_CORRECTION > ORM_MODEL > DB_COMMENT > AUTO_INTROSPECT | 待开发 |

### 6.3 业务规则存储

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.3.1 | BusinessRuleStore 类 | `src/knowledge/business_rules.py` | 业务规则存储: metric / filter / enum / period | 待开发 |
| 6.3.2 | initialize() | 同上 | 启动时扫描 docs/metrics/ 目录 | 待开发 |
| 6.3.3 | search_business_rules() | 同上 | 向量检索 Top-K 条相关规则（过滤 category="business_rule"） | 待开发 |
| 6.3.4 | _index_metric_doc() | 同上 | 解析 Markdown 文档，按 ## 标题切片，写入 ChromaDB | 待开发 |
| 6.3.5 | _split_by_headings() | 同上 | 按 Markdown 标题将文档拆分为独立 chunks | 待开发 |
| 6.3.6 | _extract_metric_tags() | 同上 | 从文档中提取标签关键词 | 待开发 |

### 6.4 缓存刷新

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.4.1 | CacheRefresher 类 | `src/knowledge/cache_refresher.py` | 定期刷新自动拉取的缓存 | 待开发 |
| 6.4.2 | refresh_expired() | 同上 | 清理 source=auto 且超过 7 天未更新的条目 | 待开发 |
| 6.4.3 | refresh_on_schema_change() | 同上 | DDL 变更监听 → 主动刷新 (轮询 INFORMATION_SCHEMA.TABLES UPDATE_TIME) | 待开发 |
| 6.4.4 | DDL 触发器/CDC 集成 (远期) | 同上 | 接入数据库原生 DDL 变更通知 | 待开发 | P3 |
| 6.4.5 | Redis 分布式锁 | 同上 | SETNX 防止多请求同时触发同一表的缓存刷新 | 待开发 | P1 |

### 6.5 枚举值发现

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.5.1 | _auto_discover_enum_values() | `src/knowledge/enum_discovery.py` | 对低基数列采样枚举值 (SELECT DISTINCT ... LIMIT 50) | 待开发 |
| 6.5.2 | 低基数判断 | 同上 | 唯一值 ≤ 20 → 可能是枚举 | 待开发 |
| 6.5.3 | TTL 设置 | 同上 | 枚举值 TTL=1天 (比表结构变化快) | 待开发 |

### 6.6 文档加载器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.6.1 | Markdown 文档扫描 | `src/knowledge/doc_loader.py` | 递归扫描 docs/metrics/ 目录下所有 *.md 文件 | 待开发 |
| 6.6.2 | YAML frontmatter 解析 | 同上 | 解析 Markdown 文件中的 YAML 元数据 (tags / category / tables) | 待开发 |
| 6.6.3 | 按标题切片 | 同上 | 按 ## 将文档拆分为独立索引单元 | 待开发 |
| 6.6.4 | ChromaDB 批量写入 | 同上 | Document 列表写入 ChromaDB collection | 待开发 |

---

## 7. 记忆系统 (memory/) `[P0:10 P1:14 P2:4 P3:2]`

### 7.1 Checkpointer

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.1.1 | PostgresSaver 配置 | `src/memory/checkpointer.py` | 生产环境 PostgreSQL checkpointer 初始化 + setup() | 待开发 |
| 7.1.2 | MemorySaver 配置 | 同上 | 开发环境内存 checkpointer (用于测试) | 待开发 |
| 7.1.3 | checkpointer 工厂函数 | 同上 | get_checkpointer() — 根据环境变量自动选择 PostgresSaver / MemorySaver | 待开发 |

### 7.2 短期记忆

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.2.1 | SessionContext dataclass | `src/memory/checkpointer.py` | session_id / thread_id / user_id / created_at / conversation_history / current_datasource / current_tables / last_sql / last_result_summary | 待开发 |
| 7.2.2 | ConversationTurn dataclass | 同上 | turn_id / user_query / generated_sql / execution_success / analysis_summary / chart_type / timestamp | 待开发 |
| 7.2.3 | 会话恢复 | 同上 | `app.aget_state(config)` — 通过 thread_id 恢复历史会话状态 | 待开发 |
| 7.2.4 | 超时归档 (30分钟) | `src/memory/session_archive.py` | 超过 30 分钟未活动的会话 → 摘要后移入 sessions_archive 表 | 待开发 |
| 7.2.5 | 轮次限制 (50轮) | 同上 | 单会话 > 50 轮 → 自动摘要前 20 轮为概括文本 | 待开发 |
| 7.2.6 | on_session_start() | 同上 | 会话启动钩子: 加载用户偏好 + 检索相关长期记忆 | 待开发 |
| 7.2.7 | archive_sessions() | 同上 | 归档超过 30 天的 inactive 会话 checkpoint | 待开发 |
| 7.2.8 | summarize_session() | 同上 | LLM 对完整会话生成摘要文本用于归档 | 待开发 |

### 7.3 长期记忆

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.3.1 | MemoryType Enum | `src/memory/long_term_store.py` | USER_PREFERENCE / SQL_TEMPLATE / LEARNED_PATTERN / CORRECTION / PROJECT_RULE | 待开发 |
| 7.3.2 | LongTermMemory dataclass | 同上 | id / memory_type / scope / content / payload / embedding / created_at / last_accessed_at / access_count / confidence / ttl_days | 待开发 |
| 7.3.3 | LongTermMemoryStore 类 | 同上 | 封装 ChromaDB + PostgreSQL 双写 | 待开发 |
| 7.3.4 | search() | 同上 | 语义检索 + 置信度过滤 (confidence >= 0.3) + memory_type 过滤 | 待开发 |
| 7.3.5 | save_sql_template() | 同上 | 保存 SQL 模板: verified=True → confidence=0.9, 否则 0.5 | 待开发 |
| 7.3.6 | save_correction() | 同上 | 保存用户纠正记录: confidence=0.95 | 待开发 |
| 7.3.7 | save_preference() | 同上 | 保存用户偏好: confidence=1.0 | 待开发 |
| 7.3.8 | get_preferences() | 同上 | 获取用户所有偏好 (PostgreSQL 精确查询，不走向量) | 待开发 |
| 7.3.9 | _upsert() | 同上 | 幂等写入 ChromaDB + PostgreSQL | 待开发 |
| 7.3.10 | _to_memory() | 同上 | 将 ChromaDB 检索结果转为 LongTermMemory | 待开发 |
| 7.3.11 | _upsert() 双写事务保证 | 同上 | 先 PG 后 ChromaDB；PG 成功 + ChromaDB 失败 → 写入 pending_vector_sync 表，后台补偿重试 | 待开发 | P1 |

### 7.4 记忆维护

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.4.1 | MemoryMaintenance 类 | `src/memory/long_term_store.py` | 定期维护任务调度 | 待开发 |
| 7.4.2 | decay_old_templates() | 同上 | 30 天未使用的 SQL 模板置信度 * 0.5 | 待开发 |
| 7.4.3 | prune_low_confidence() | 同上 | 删除 confidence < 0.3 且 access_count = 0 的自动模板 | 待开发 |

### 7.5 上下文裁剪

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 7.5.1 | build_llm_context() | `src/memory/context_builder.py` | 统一上下文裁剪: 热数据(3轮完整) → 温数据(4-10轮摘要) → 冷数据(ChromaDB 检索) | 待开发 |
| 7.5.2 | _summarize_turns() | 同上 | 用小模型将多轮对话压缩为 1-2 句摘要 | 待开发 |
| 7.5.3 | 各 Node 上下文裁剪集成 | 各 Node 文件 | generate_sql / analyze_result / generate_chart Node 调用 build_llm_context() | 待开发 |
| 7.5.4 | Prompt token 预算检查 | 同上 | 确保每次 LLM 调用 ≤ 7000 tokens | 待开发 |
| 7.5.5 | 异步预计算摘要 | 同上 | Phase 2: 会话归档时异步调用 cheap_llm 预计算摘要存入 ConversationTurn.summary；Phase 1 用规则拼接 | 待开发 | P1 |

---

## 8. MCP 集成 (mcp/) `[P0:4 P1:8 P2:9 P3:2]`

### 8.1 MCP Client

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.1.1 | MCPClientManager 类 | `src/mcp/client_manager.py` | 管理所有 MCP Client 连接的生命周期 | 待开发 |
| 8.1.2 | connect_all() | 同上 | 启动时并发连接 config/mcp_servers.yaml 中所有 MCP Server | 待开发 |
| 8.1.3 | _connect_single() | 同上 | 连接单个 MCP Server (支持 stdio + SSE transport) | 待开发 |
| 8.1.4 | _resolve_env() | 同上 | 解析 MCP 配置中的 ${VAR_NAME} 环境变量占位符 | 待开发 |
| 8.1.5 | _mcp_to_langchain_tool() | 同上 | 将 MCP Tool 适配为 LangChain StructuredTool (加 namespace 前缀) | 待开发 |
| 8.1.6 | _build_schema() | 同上 | 从 MCP Tool 的 JSONSchema inputSchema 生成 Pydantic args_schema | 待开发 |
| 8.1.7 | get_all_tools() | 同上 | 返回所有 MCP 转换来的 LangChain Tool 列表 | 待开发 |
| 8.1.8 | health_check() | 同上 | 定期 ping 所有 MCP Server，断线自动重连 | 待开发 |
| 8.1.9 | _reconnect() | 同上 | 单个 MCP Server 的断线重连逻辑 (指数退避) | 待开发 |
| 8.1.10 | close_all() | 同上 | 关闭所有连接 (AsyncExitStack.aclose) | 待开发 |
| 8.1.11 | _sse_client() | `src/mcp/tool_adapter.py` | SSE transport 的客户端实现 | 待开发 |
| 8.1.12 | 降级策略 | `src/mcp/client_manager.py` | 重连 5 次失败 → 标记 degraded → 从 get_all_tools() 移除 → 健康检查恢复后自动启用 | 待开发 | P1 |

### 8.2 MCP Server

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.2.1 | FastMCP 实例化 | `src/mcp/server.py` | FastMCP("data-analysis-agent") 创建 MCP Server | 待开发 |
| 8.2.2 | query_database Tool | 同上 | 以自然语言查询数据库，返回分析结果与图表 | 待开发 |
| 8.2.3 | list_datasources Tool | 同上 | 列出当前所有可用数据源及描述 | 待开发 |
| 8.2.4 | get_table_schema Tool | 同上 | 获取指定表的完整结构信息 | 待开发 |
| 8.2.5 | get_metrics Tool | 同上 | 查询业务指标口径定义和计算公式 | 待开发 |
| 8.2.6 | MCP Server 启动入口 | `src/mcp/__main__.py` | `python -m src.mcp` 启动 MCP Server | 待开发 |
| 8.2.7 | Claude Code 集成配置 | `claude_code_mcp.json` | 配置为 Claude Code 可调用的 MCP Server | 待开发 |

### 8.3 MCP Agent Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.3.1 | mcp_agent_node() | `src/graph/workflow.py` | 使用 create_react_agent 为文件分析场景创建动态工具调用 Node | 待开发 |
| 8.3.2 | route_by_intent() 集成 | 同上 | intent == "file_analysis" → 路由到 mcp_agent Node | 待开发 |
| 8.3.3 | MCP Agent system prompt | `src/llm/prompts.py` | "你是一个数据分析助手，可以访问文件系统和外部知识库" | 待开发 |

---

## 9. Skills 技能系统 `[P0:4 P1:6 P2:5 P3:3]`

### 9.1 Skill 引擎

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.1.1 | Skill dataclass | `src/skill_manager.py` | name / version / description / triggers / depends_on / tools / system_prompt_override / output_schema_extension / source_path / enabled | 待开发 |
| 9.1.2 | SkillManager 类 | `src/skill_manager.py` | Skill 发现、加载、激活与生命周期管理 | 待开发 |
| 9.1.3 | discover() | 同上 | 启动时扫描 skills/ 目录，发现所有 SKILL.md | 待开发 |
| 9.1.4 | _parse_skill_manifest() | 同上 | 解析 SKILL.md 的 YAML frontmatter + Markdown body | 待开发 |
| 9.1.5 | _check_dependencies() | 同上 | 检查依赖 (mcp_servers / skills / python_packages) 是否满足 | 待开发 |
| 9.1.6 | match_skills() | 同上 | 根据用户输入匹配激活 Skill: 关键词 + 意图 + 表名三重 OR 匹配 | 待开发 |
| 9.1.7 | get_active_tools() | 同上 | 动态加载激活 Skill 的 tools.py 模块，获取 BaseTool 列表 | 待开发 |
| 9.1.8 | build_skill_prompt() | 同上 | 组装激活 Skill 的 system_prompt_override 追加到 System Prompt | 待开发 |
| 9.1.9 | _load_skill_module() | 同上 | 动态 import Skill 的 tools.py 模块 | 待开发 |

### 9.2 示例 Skill — 数据质量检查

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.2.1 | SKILL.md | `skills/data_quality_check/SKILL.md` | Skill 清单 (YAML frontmatter + 指令) | 待开发 |
| 9.2.2 | check_null_rate Tool | `skills/data_quality_check/tools.py` | 检查指定列的空值率 | 待开发 |
| 9.2.3 | check_duplicates Tool | 同上 | 检查指定列的重复值 | 待开发 |
| 9.2.4 | detect_outliers Tool | 同上 | Z-Score 异常值检测 | 待开发 |
| 9.2.5 | PROMPTS 定义 | `skills/data_quality_check/prompts.py` | Skill 专属 Prompt 模板 | 待开发 |

### 9.3 示例 Skill — 自定义报告

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.3.1 | SKILL.md | `skills/custom_report/SKILL.md` | Skill 清单 | 待开发 |
| 9.3.2 | 周报模板 | `skills/custom_report/templates/weekly_report.jinja2` | Jinja2 模板 — 周度数据报告 | 待开发 |
| 9.3.3 | 月报模板 | 同上 | Jinja2 模板 — 月度数据报告 | 待开发 |
| 9.3.4 | 报告渲染工具 | `skills/custom_report/tools.py` | render_report(template_name, data) → 渲染 HTML/PDF | 待开发 |

### 9.4 Skill 分发

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.4.1 | 本地 Skill 扫描 | `src/skill_manager.py` | skills/*/SKILL.md 目录扫描 | 待开发 |
| 9.4.2 | Git 子模块支持 | `.gitmodules` | skills/community/ 下的社区 Skill 用 git submodule 引入 | 待开发 |
| 9.4.3 | Skill Registry 接口 (远期) | `src/skill_manager.py` | 中心化 Skill 市场接口预留 | 待开发 |

---

## 10. LLM 管理层 (llm/) `[P0:10 P1:4]`

### 10.1 客户端工厂

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 10.1.1 | ChatOpenAI 工厂 | `src/llm/client.py` | get_openai_llm(temperature, model) — 从 Settings 读取 API Key，创建 ChatOpenAI 实例 | 待开发 |
| 10.1.2 | ChatAnthropic 工厂 | 同上 | get_anthropic_llm(temperature, model) — 创建 ChatAnthropic 实例 | 待开发 |
| 10.1.3 | LLM 路由器 | 同上 | get_llm(provider: str) — 根据配置自动选择 OpenAI / Anthropic | 待开发 |
| 10.1.4 | cheap_llm 工厂 | 同上 | get_cheap_llm() — 用于摘要等低复杂度任务的低成本模型 | 待开发 |
| 10.1.5 | 模型参数配置 | `src/config.py` | LLM_MODEL / LLM_TEMPERATURE / LLM_MAX_TOKENS / LLM_TIMEOUT | 待开发 |

### 10.2 Prompt 模板

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 10.2.1 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别 Prompt | 待开发 |
| 10.2.2 | SQL_GENERATION_SYSTEM_PROMPT | 同上 | SQL 生成系统 Prompt (含 {datasource_type} / {dialect} 占位) | 待开发 |
| 10.2.3 | DATA_ANALYSIS_PROMPT | 同上 | 数据分析 Prompt (摘要 / 洞察 / 图表 / 追问) | 待开发 |
| 10.2.4 | CHART_RECOMMEND_PROMPT | 同上 | 图表推荐 Prompt | 待开发 |
| 10.2.5 | RESPONSE_BUILD_PROMPT | 同上 | 响应组装 Prompt | 待开发 |
| 10.2.6 | SUMMARIZE_SESSION_PROMPT | 同上 | 会话摘要 Prompt | 待开发 |
| 10.2.7 | 方言速查块注入 | 同上 | get_dialect_prompt(dialect) — 返回对应数据库的函数速查表 | 待开发 |
| 10.2.8 | Prompt 版本号管理 | 同上 | 每个 Prompt 模板附带 VERSION 常量，支持 A/B 测试 | 待开发 |

---

## 11. API 层 (api/)

### 11.1 核心接口

| # | 功能 | 文件 | 路由 | 描述 | 状态 |
|---|------|------|------|------|------|
| 11.1.1 | POST 分析查询 | `src/api/routes.py` | `POST /api/v1/chat` | 发送自然语言查询，返回完整分析结果 (SQL + 数据 + 分析 + 图表) | 待开发 |
| 11.1.2 | POST 流式查询 | `src/api/routes.py` | `POST /api/v1/chat/stream` | SSE 流式返回分析过程 (Node 级进度 + LLM token 级输出) | 待开发 |
| 11.1.3 | GET 表列表 | `src/api/routes.py` | `GET /api/v1/schema/tables` | 获取指定数据源的所有表 | 待开发 |
| 11.1.4 | GET 表结构 | `src/api/routes.py` | `GET /api/v1/schema/tables/{table_name}` | 获取指定表的结构信息 | 待开发 |
| 11.1.5 | POST Schema 刷新 | `src/api/routes.py` | `POST /api/v1/schema/refresh` | 手动刷新 Schema 缓存 | 待开发 |
| 11.1.6 | GET 会话历史 | `src/api/routes.py` | `GET /api/v1/history?session_id=xxx` | 获取指定会话的对话历史 | 待开发 |
| 11.1.7 | POST 注册数据源 | `src/api/routes.py` | `POST /api/v1/datasources` | 外挂模式: 注册新数据源 | 待开发 |
| 11.1.8 | DELETE 删除数据源 | `src/api/routes.py` | `DELETE /api/v1/datasources/{name}` | 外挂模式: 移除数据源 | 待开发 |
| 11.1.9 | GET 数据源列表 | `src/api/routes.py` | `GET /api/v1/datasources` | 列出所有数据源 | 待开发 |
| 11.1.10 | GET 健康检查 | `src/api/routes.py` | `GET /api/v1/health` | 检查服务健康状况 (DB 连接 / ChromaDB 状态 / MCP 连接) | 待开发 |
| 11.1.11 | PUT 字段标注 | `src/api/routes.py` | `PUT /api/v1/schema/tables/{table}/columns/{column}/comment` | 手动标注字段中文说明，直接写入 ChromaDB | 待开发 | P1 |
| 11.1.12 | POST MCP 重置 | `src/api/routes.py` | `POST /api/v1/mcp/{name}/reset` | 手动重置 degraded 状态的 MCP Server | 待开发 | P1 |
| 11.1.13 | GET 指标列表 | `src/api/routes.py` | `GET /api/v1/metrics` | 查询已注册的指标口径列表 | 待开发 | P2 |

### 11.2 分页增强

| # | 功能 | 文件 | 路由 | 描述 | 状态 |
|---|------|------|------|------|------|
| 11.2.1 | 表列表分页 | `src/api/routes.py` | `GET /api/v1/schema/tables?page=1&page_size=20&search=xxx` | 表列表分页 + 搜索 | 待开发 | P1 |
| 11.2.2 | 会话历史分页 | `src/api/routes.py` | `GET /api/v1/history?session_id=xxx&page=1&page_size=20` | 会话历史分页查询 | 待开发 | P1 |
| 11.2.3 | 数据源列表分页 | `src/api/routes.py` | `GET /api/v1/datasources?page=1&page_size=20` | 数据源列表分页查询 | 待开发 | P1 |

### 11.3 请求/响应 Schema (重新编号为 11.3)

### 11.2 请求/响应 Schema

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.2.1 | ChatRequest Pydantic model | `src/api/schemas.py` | session_id / query / datasource | 待开发 |
| 11.2.2 | ChatResponse Pydantic model | 同上 | session_id / query / sql / data / analysis / chart | 待开发 |
| 11.2.3 | AnalysisResult Pydantic model | 同上 | summary / insights / recommended_chart_type / follow_up_questions | 待开发 |
| 11.2.4 | ChartConfig Pydantic model | 同上 | type / echarts_option | 待开发 |
| 11.2.5 | DataSourceCreateRequest | 同上 | 注册数据源请求体 | 待开发 |
| 11.2.6 | ErrorResponse | 同上 | error_code / error_message / suggestion / retry_count | 待开发 |
| 11.2.7 | HealthResponse | 同上 | status / db_connected / chroma_ok / mcp_servers_status | 待开发 |

### 11.3 流式输出

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.3.1 | stream_analysis() | `src/api/streaming.py` | FastAPI SSE endpoint: astream_events 循环推送 | 待开发 |
| 11.3.2 | on_chat_model_stream 事件处理 | 同上 | LLM token 级别流式推送 | 待开发 |
| 11.3.3 | on_chain_start 事件处理 | 同上 | Node 开始执行通知 | 待开发 |
| 11.3.4 | on_chain_end 事件处理 | 同上 | Node 执行完成通知 (含 output) | 待开发 |
| 11.3.5 | SSE 格式化 | 同上 | `data: {json}\n\n` 格式包装 | 待开发 |

---

## 12. 安全模块 `[P0:8 P1:4 P2:2]`

### 12.1 SQL 安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.1.1 | DDL/DML 正则黑名单 | `src/graph/nodes/layer3_validate.py` | 拦截 INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/RENAME/GRANT/REVOKE/MERGE/REPLACE | 待开发 |
| 12.1.2 | 危险函数拦截 | 同上 | 拦截 sleep() / benchmark() / 存储过程调用 | 待开发 |
| 12.1.3 | 白名单模式 | 同上 | 默认只允许 SELECT / SHOW / DESCRIBE / EXPLAIN | 待开发 |
| 12.1.4 | 只读数据库账号 | 各 Connector | 所有数据源连接使用只读账号 | 待开发 |
| 12.1.5 | SQL 注入防护 | 同上 | LLM 输出的 SQL 已结构化，不拼接用户输入 | 待开发 |
| 12.1.6 | LLM 输出二次校验 | `src/graph/nodes/generate_sql.py` | SQL 中引用的表名/字段名必须在 state["relevant_tables"] 中存在，拦截 LLM 幻觉 | 待开发 | P1 |

### 12.2 限流控制

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.2.1 | 单用户每小时查询上限 | `src/graph/nodes/execute_sql.py` | Redis 计数器 + 滑动窗口 | 待开发 |
| 12.2.2 | 单次查询最大扫描行数 | `src/config.py` | 配置项 MAX_SCAN_ROWS (默认 1000 万) | 待开发 |
| 12.2.3 | 单次查询最大执行时间 | `src/config.py` | 配置项 MAX_EXECUTION_TIME (默认 30 秒) | 待开发 |
| 12.2.4 | 结果集最大返回行数 | `src/config.py` | 配置项 MAX_RESULT_ROWS (默认 10 万) | 待开发 |

### 12.3 数据安全

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 12.3.1 | 查询结果脱敏 | `src/security/data_masker.py` | 自动识别并脱敏手机号、身份证号、邮箱 | 待开发 |
| 12.3.2 | 敏感表/字段白名单 | 同上 | 可配置的敏感字段访问控制 | 待开发 |
| 12.3.3 | 查询审计日志 | 同上 | 完整记录: 时间 / 用户 / 数据源 / SQL / 结果行数 / 执行耗时 | 待开发 |

---

## 13. 数据分析引擎 `[P1:6]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 13.1 | compute_statistics() | `src/tools/analyzer.py` | 对所有数值列计算: 均值/中位数/标准差/分位数/空值率/唯一值数 | 待开发 |
| 13.2 | compute_trend() | 同上 | 时间序列: 同比/环比/移动平均/线性回归斜率 | 待开发 |
| 13.3 | detect_outliers_zscore() | 同上 | Z-Score 方法: \|z\| > 3 标记为异常 | 待开发 |
| 13.4 | detect_outliers_iqr() | 同上 | IQR 方法: Q1-1.5*IQR < x < Q3+1.5*IQR | 待开发 |
| 13.5 | compute_concentration() | 同上 | 帕累托分析: Top N 集中度占比 | 待开发 |
| 13.6 | compute_correlation() | 同上 | 数值列相关性矩阵 (Pearson) | 待开发 |

---

## 14. 可视化引擎 `[P1:6 P2:2]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 14.1 | 智能选图 classify_chart_type() | `src/tools/chart_generator.py` | 时间+数值→line / 分类+数值→bar / 占比→pie / 双数值→scatter / 高维→heatmap / 通用→table | 待开发 |
| 14.2 | ECharts 折线图生成 | 同上 | 生成 line chart 的 ECharts option JSON | 待开发 |
| 14.3 | ECharts 柱状图生成 | 同上 | 生成 bar chart 的 ECharts option JSON | 待开发 |
| 14.4 | ECharts 饼图生成 | 同上 | 生成 pie chart 的 ECharts option JSON | 待开发 |
| 14.5 | ECharts 散点图生成 | 同上 | 生成 scatter chart 的 ECharts option JSON | 待开发 |
| 14.6 | ECharts 热力图生成 | 同上 | 生成 heatmap chart 的 ECharts option JSON | 待开发 |
| 14.7 | 表格渲染 | 同上 | 生成 Markdown 表格或 HTML table | 待开发 |
| 14.8 | 用户调整图表 | 同上 | 自然语言指令 "用饼图展示" → 重新生成图表 | 待开发 |

---

## 15. 评估与质量保障 `[P1:2 P2:4]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 15.1 | NL2SQL 标注数据集 | `tests/fixtures/nl2sql_benchmark.json` | (question, tables, expected_sql, expected_analysis) 四元组 | 待开发 |
| 15.2 | SQL 正确性 evaluator | `tests/evaluators/sql_correctness.py` | sqlparse 标准化后比对 | 待开发 |
| 15.3 | SQL 安全拦截 evaluator | `tests/evaluators/sql_security.py` | 注入危险 SQL 的测试用例集 | 待开发 |
| 15.4 | LangSmith aevaluate 集成 | `tests/evaluators/run_eval.py` | 批量回归测试，LangSmith Dataset 驱动 | 待开发 |
| 15.5 | Schema 检索命中率评估 | `tests/evaluators/schema_recall.py` | Top-5 召回率测量 | 待开发 |
| 15.6 | CI 自动化测试 | `.github/workflows/test.yml` | GitHub Actions: 单元测试 + 集成测试 + 评估 | 待开发 |

---

## 16. 测试 `[P1:5 P2:10 P3:2]`

### 16.1 单元测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.1.1 | KnowledgeEntry / SchemaSnapshot 序列化测试 | `tests/test_datasource/test_schema_snapshot.py` | 测试 to_prompt_text() 格式正确 | 待开发 |
| 16.1.2 | DataSourceConfig 验证测试 | `tests/test_datasource/test_config.py` | 测试必填字段校验 | 待开发 |
| 16.1.3 | sqlglot validator 测试 | `tests/test_tools/test_sqlglot_validator.py` | 测试各种 SQL 错误的拦截和函数建议 | 待开发 |
| 16.1.4 | SQL 安全拦截测试 | `tests/test_tools/test_sql_security.py` | Drop/Delete/Insert 语句拦截验证 | 待开发 |
| 16.1.5 | compute_statistics() 测试 | `tests/test_tools/test_analyzer.py` | pandas 统计计算正确性 | 待开发 |
| 16.1.6 | classify_chart_type() 测试 | `tests/test_tools/test_chart_generator.py` | 各种列组合的选图正确性 | 待开发 |
| 16.1.7 | LongTermMemoryStore 测试 | `tests/test_memory/test_long_term_store.py` | CRUD + 置信度过滤 + 语义检索 | 待开发 |
| 16.1.8 | build_llm_context() 测试 | `tests/test_memory/test_context_builder.py` | 三层裁剪逻辑验证 | 待开发 |
| 16.1.9 | SkillManager 测试 | `tests/test_skills/test_skill_manager.py` | discover / match_skills / build_skill_prompt | 待开发 |
| 16.1.10 | MCPClientManager 测试 | `tests/test_mcp/test_client_manager.py` | 使用 mock MCP Server 测试连接/转换/重连 | 待开发 |
| 16.1.11 | LLM 输出二次校验测试 | `tests/test_tools/test_sql_security.py` | 模拟 LLM 幻觉编造表名/字段名 → 验证拦截 | 待开发 | P1 |

### 16.2 Mock 工具与测试基础设施

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.2.1 | FakeListChatModel 工厂 | `tests/fixtures/mock_llm.py` | 按顺序返回预设 AIMessage，用于所有 LLM Node 测试 | 待开发 | P1 |
| 16.2.2 | SQLite MemoryDB Connector | `tests/fixtures/mock_db.py` | 基于 `aiosqlite` + `sqlalchemy` 的内存数据库连接器，用于 mock 真实 DB | 待开发 | P1 |
| 16.2.3 | StaticMCPTestServer | `tests/fixtures/mock_mcp.py` | 注册固定 tools 的内存 MCP Server，不依赖外部进程 | 待开发 | P1 |
| 16.2.4 | ChromaDB EphemeralClient | `tests/conftest.py` | 测试用临时向量库，`pytest.fixture` 自动创建销毁 | 待开发 | P1 |

### 16.3 集成测试

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 16.3.1 | 完整 LangGraph 工作流测试 | `tests/test_graph/test_workflow.py` | Mock LLM + SQLite 内存 DB，端到端走通 | 待开发 | P1 |
| 16.3.2 | SQL 错误重试集成测试 | 同上 | 模拟执行失败 → 验证回到 generate_sql 并最终成功 | 待开发 | P1 |
| 16.3.3 | 安全拦截终止流程测试 | 同上 | 模拟生成 Delete SQL → 验证直接返回错误不进入执行 | 待开发 | P1 |
| 16.3.4 | 三级 Schema 回退集成测试 | `tests/test_knowledge/test_schema_manager.py` | Mock 空缓存 → 验证走到 DB 内省兜底 | 待开发 | P1 |
| 16.3.5 | API 集成测试 | `tests/test_api/test_routes.py` | httpx AsyncClient + FastAPI TestClient 测试所有接口 | 待开发 | P1 |
| 16.3.6 | 条件边路由测试 | `tests/test_graph/test_routing.py` | 构造特定 AnalysisState，验证 after_layer3 / should_retry 返回值 | 待开发 | P1 |

---

## 17. 基础设施与运维 `[P0:4 P1:2 P2:7 P3:2]`

### 17.1 数据库存储

| # | 功能 | 表名 | 描述 | 状态 |
|---|------|------|------|------|
| 17.1.1 | checkpointer 表 | `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` | LangGraph PostgresSaver 自动创建 | 待开发 |
| 17.1.2 | 会话表 | `active_sessions` | session_id / thread_id / user_id / created_at / last_active_at | 待开发 |
| 17.1.3 | 会话归档表 | `sessions_archive` | thread_id / summary / archived_at | 待开发 |
| 17.1.4 | 长期记忆表 | `long_term_memories` | id / memory_type / scope / content / payload / created_at / last_accessed_at / access_count / confidence | 待开发 |
| 17.1.5 | 数据源配置表 | `datasource_configs` | 外挂模式数据源配置持久化 (name/dialect/host/port/database/username/encrypted_password) | 待开发 |
| 17.1.6 | 查询审计日志表 | `query_audit_log` | user_id / datasource / sql / row_count / execution_time_ms / created_at | 待开发 |

### 17.2 数据库迁移

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 17.2.1 | 初始迁移 | `migrations/001_initial.py` | 创建所有业务表 (sessions / long_term_memories / datasource_configs / audit_log) | 待开发 |
| 17.2.2 | 迁移工具 | `src/db/migrations.py` | Alembic 或自研轻量迁移执行器 | 待开发 |

### 17.3 监控与可观测性

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.3.1 | LangSmith 全链路追踪 | 每个 Node 的输入/输出/延迟/LLM token 自动上报 | 待开发 |
| 17.3.2 | Prometheus metrics | 请求数 / 错误率 / P50/P95/P99 延迟 / LLM token 消耗 | 待开发 |
| 17.3.3 | Grafana Dashboard | 服务健康 / 查询性能 / 错误趋势 / 成本追踪 | 待开发 |
| 17.3.4 | 结构化日志 | structlog JSON 格式输出，支持 ELK / Loki 采集 | 待开发 |

### 17.4 容器化

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 17.4.1 | Dockerfile | 多阶段构建 (builder + runtime) | 待开发 |
| 17.4.2 | docker-compose.yml | PostgreSQL 17 + ChromaDB + Redis 7 + App 开发环境编排 | 待开发 |
| 17.4.3 | .dockerignore | 排除 .venv / __pycache__ / .git / tests / .claude | 待开发 |

---

## 18. 前端 (Phase 3) `[P2:10]`

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 18.1 | React + TypeScript 项目初始化 | Vite 脚手架 | 待开发 |
| 18.2 | Chat 对话界面 | 消息列表 + 输入框 + 发送按钮 | 待开发 |
| 18.3 | SQL 代码高亮展示 | 生成的 SQL 以代码块显示，支持复制 | 待开发 |
| 18.4 | 数据表格展示 | 查询结果以可排序/可筛选的表格展示 | 待开发 |
| 18.5 | ECharts 图表渲染 | 根据 chart_config 在前端渲染交互式图表 | 待开发 |
| 18.6 | 流式进度展示 | SSE 接收 Node 级进度，展示 "正在生成SQL → 正在执行 → 正在分析" 状态 | 待开发 |
| 18.7 | 数据源管理页面 | 列表 / 新增 / 删除数据源的表单页面 | 待开发 |
| 18.8 | 字段标注页面 | 给字段补充中文说明的管理页面 | 待开发 |
| 18.9 | 查询历史页面 | 展示用户自己的历史查询和结果 | 待开发 |
| 18.10 | 指标文档管理页面 | 在线编辑业务规则 Markdown 文档 | 待开发 |

---

## 19. 扩展能力 (Phase 4) `[P3:10]`

| # | 功能 | 描述 | 状态 |
|---|------|------|------|
| 19.1 | 自动 Insight 发现 | 定期 (每小时/每天) 扫描数据，主动推送异常和趋势变化 | 待开发 |
| 19.2 | 定时报告 | 按日/周/月自动生成分析报告并推送 (邮件/飞书/Slack) | 待开发 |
| 19.3 | 知识库自进化 | 高质量 SQL 自动积累，从 AUTO_INTROSPECT 升级到 LEARNED_PATTERN | 待开发 |
| 19.4 | 多模态输入 | 上传 Excel/CSV 文件 → 自动建临时表 → 纳入分析 | 待开发 |
| 19.5 | 权限体系 | 多用户注册/登录，RBAC 角色管理 (管理员/分析师/只读) | 待开发 |
| 19.6 | 多语言自然语言支持 | 英文输入 → 自动翻译 → 分析 → 英文输出 | 待开发 |
| 19.7 | 飞书/Slack 集成 | Bot 接入消息平台，@机器人 即可查询数据 | 待开发 |
| 19.8 | Grafana 数据源 | 将智能体暴露为 Grafana 自定义数据源 | 待开发 |
| 19.9 | 一键部署脚本 | `curl \| bash` 式一键部署脚本 | 待开发 |
| 19.10 | 自动优化建议 | 识别慢查询 → 推荐索引 / 改写 / 物化视图方案 | 待开发 |

---

## 优先级分配总览

| 模块 | P0 | P1 | P2 | P3 | 小计 | 去重标注 |
|------|----|----|----|----|------|---------|
| 1. 项目基础设施 | 10 | 4 | — | — | 14 | — |
| 2. 数据源管理 | 20 | 12 | 3 | 1 | 36 | — |
| 3. 数据库连接器 | 8 | 2 | 4 | — | 14 | — |
| 4. LangGraph 编排引擎 | 28 | 16 | 3 | 1 | 48 | 4.10.4 → 见 7.3.5 |
| 5. 工具层 | 11 | 2 | — | — | 13 | — |
| 6. 知识库管理 | 14 | 10 | 4 | 1 | 29 | 6.3.4 与 6.6.3 协同 |
| 7. 记忆系统 | 10 | 14 | 4 | 2 | 30 | — |
| 8. MCP 集成 | 4 | 8 | 9 | 2 | 23 | — |
| 9. Skills 技能系统 | 4 | 6 | 5 | 3 | 18 | — |
| 10. LLM 管理层 | 10 | 4 | — | — | 14 | — |
| 11. API 层 | 10 | 10 | 2 | — | 22 | — |
| 12. 安全模块 | 8 | 4 | 2 | — | 14 | — |
| 13. 数据分析引擎 | — | 6 | — | — | 6 | — |
| 14. 可视化引擎 | — | 6 | 2 | — | 8 | — |
| 15. 评估与质量保障 | — | 2 | 4 | — | 6 | — |
| 16. 测试 | — | 5 | 10 | 2 | 17 | — |
| 17. 基础设施与运维 | 4 | 2 | 7 | 2 | 15 | 17.1.1 LangGraph 自动管理 |
| 18. 前端 | — | — | 10 | — | 10 | — |
| 19. 扩展能力 | — | — | — | 10 | 10 | — |
| **总计** | **141** | **113** | **69** | **24** | **347** | — |

> 优先级分配依据 SPEC.md §8 实现路线图的四个 Phase：
> - P0 = Phase 1 MVP（核心链路）
> - P1 = Phase 2 增强（正确性 + 体验）
> - P2 = Phase 3 生产化（多数据源/流式/容器化）
> - P3 = Phase 4 进阶（远期规划）

---

## 关键依赖关系图

以下列出跨模块的硬依赖。编号为 `模块.功能序号`。

```
Phase 1 核心链路:
  1.1.1 Poetry 项目 ──┬→ 2.1.1 DataSourceConfig
                      ├→ 10.1.1 ChatOpenAI 工厂
                      └→ 4.1.1 AnalysisState

  2.1.2 DataSourceRegistry ──→ 3.1.1 ConnectorBase
  2.5.x DB 内省 ────────────→ 3.1.1 ConnectorBase
  2.5.x DB 内省 ────────────→ 6.1.2 get_or_fetch_schema()
  2.6.1 SchemaSnapshot ──────→ 4.3 retrieve_schema Node

  4.1.1 AnalysisState ──────→ 4.2 ~ 4.10 所有 Node
  4.1.2 StateGraph 组装 ────→ 4.2 ~ 4.10 所有 Node
  4.2 classify_intent ──────→ 10.2.1 INTENT_CLASSIFY_PROMPT
  4.3 retrieve_schema ──────→ 6.1.2 get_or_fetch_schema()
  4.3 retrieve_schema ──────→ 6.3.3 search_business_rules()
  4.3 retrieve_schema ──────→ 7.3.4 search() 长期记忆
  4.4 generate_sql ─────────→ 10.2.2 SQL_GENERATION_SYSTEM_PROMPT
  4.4 generate_sql ─────────→ 7.5.1 build_llm_context()
  4.5 layer3_validate ──────→ 5.2.1 validate_with_sqlglot()
  4.6 layer4_explain ───────→ 3.x explain()
  4.7 execute_sql ──────────→ 2.1.2 DataSourceRegistry.resolve()
  4.7 execute_sql ──────────→ 12.2 限流控制
  4.8 analyze_result ───────→ 10.2.3 DATA_ANALYSIS_PROMPT
  4.9 generate_chart ───────→ 14.1 classify_chart_type()
  4.10 build_response ──────→ 4.4.10 SQL 模板保存 → 见 7.3.5

  3.1.1 ConnectorBase ──────→ 3.2/3.3/3.4 三个实现

  5.1.1 SchemaExplorerTool ─→ 6.1.2 get_or_fetch_schema()
  5.1.3 SQLglotValidator ───→ 5.2.1 validate_with_sqlglot()

  8.1.1 MCPClientManager ───→ config/mcp_servers.yaml (1.2.3)
  9.1.2 SkillManager ───────→ 4.2.3 Skill 匹配触发

Phase 2 关键依赖:
  11.1.2 流式查询 ──────────→ 4.1.2 StateGraph (astream_events)
  11.1.7~11.1.9 数据源API ──→ 2.3 ExternalProvider
  13.x 数据分析引擎 ─────────→ 4.8 analyze_result Node
  14.x 可视化引擎 ───────────→ 4.9 generate_chart Node
  7.2.8 summarize_session ──→ 10.1.4 cheap_llm 工厂
  7.5.1 build_llm_context ──→ 7.3.4 long_term_store.search()

Phase 3 关键依赖:
  3.3 MySQL 连接器 ──────────→ 3.1.1 ConnectorBase
  3.4 PostgreSQL 连接器 ─────→ 3.1.1 ConnectorBase
  17.3 Prometheus ───────────→ 11.1.10 GET /health
  18.x 前端全部 ─────────────→ 11.x 所有 API 完成
```

### 跨模块协同功能标注

以下功能在不同模块中被提及，但本质是同一实现：

| 功能 | 主实现位置 | 引用位置 | 说明 |
|------|-----------|---------|------|
| SQL 模板保存 | 7.3.5 save_sql_template() | 4.10.4 build_response | 4.10.4 调用 7.3.5，不独立实现 |
| Markdown 文档切片 | 6.3.5 _split_by_headings() | 6.6.3 按标题切片 | 6.3.4 是主实现，6.6.3 是 doc_loader 的复用 |
| 用户偏好存储 | 7.3.7 save_preference() | 7.2.6 on_session_start() | 7.2.6 读取 7.3.8，不独立存储 |

---

## 统计摘要

| 模块 | 功能点数量 | P0 | P1 | P2 | P3 |
|------|-----------|----|----|----|----|
| 1. 项目基础设施 | 14 | 10 | 4 | — | — |
| 2. 数据源管理 | 37 | 20 | 13 | 3 | 1 |
| 3. 数据库连接器 | 14 | 8 | 2 | 4 | — |
| 4. LangGraph 编排引擎 | 48 | 28 | 16 | 3 | 1 |
| 5. 工具层 | 13 | 11 | 2 | — | — |
| 6. 知识库管理 | 30 | 14 | 11 | 4 | 1 |
| 7. 记忆系统 | 32 | 10 | 16 | 4 | 2 |
| 8. MCP 集成 | 24 | 4 | 9 | 9 | 2 |
| 9. Skills 技能系统 | 18 | 4 | 6 | 5 | 3 |
| 10. LLM 管理层 | 14 | 10 | 4 | — | — |
| 11. API 层 | 28 | 10 | 16 | 2 | — |
| 12. 安全模块 | 15 | 8 | 5 | 2 | — |
| 13. 数据分析引擎 | 6 | — | 6 | — | — |
| 14. 可视化引擎 | 8 | — | 6 | 2 | — |
| 15. 评估与质量保障 | 6 | — | 2 | 4 | — |
| 16. 测试 | 23 | — | 11 | 10 | 2 |
| 17. 基础设施与运维 | 15 | 4 | 2 | 7 | 2 |
| 18. 前端 | 10 | — | — | 10 | — |
| 19. 扩展能力 | 10 | — | — | — | 10 |
| **总计** | **365** | **141** | **131** | **69** | **24** |
