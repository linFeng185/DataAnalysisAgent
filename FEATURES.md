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
| 1.2.3 | MCP Server 注册表 | `config/mcp_servers.yaml` | 声明外部 MCP Server | 待开发[^1] | P0 |
| 1.2.4 | 数据源配置文件 | `config/datasources.yaml` | 外挂模式数据源声明 (dialect/host/port/database/凭证引用) | 开发完成 | P0 |

### 1.3 异常与错误处理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 1.3.1 | DataSourceNotFoundError | `src/exceptions.py` | 数据源未找到异常 | 单测完成 | P0 |
| 1.3.2 | SQLValidationError | `src/exceptions.py` | SQL 校验失败异常，携带 errors/warnings 列表 | 单测完成 | P0 |
| 1.3.3 | SQLSecurityError | `src/exceptions.py` | SQL 安全拦截异常（含拦截原因和违规操作） | 单测完成 | P0 |
| 1.3.4 | ExecutionError | `src/exceptions.py` | SQL 执行失败异常，携带原始错误信息和 retry_count | 单测完成 | P0 |
| 1.3.5 | RateLimitError | `src/exceptions.py` | 请求频率超限异常 | 单测完成 | P0 |
| 1.3.6 | KnowledgeNotFoundError | `src/exceptions.py` | 知识库未找到相关知识异常 | 单测完成 | P0 |
| 1.3.7 | 全局异常处理中间件 | `src/api/middleware.py` | 7 种异常 → HTTP 响应映射 | 单测完成 | P0 |

---

## 2. 数据源管理模块 (datasource/) `[P0:20 P1:12 P2:3 P3:1]`

### 2.1 配置对象与注册

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.1.1 | DataSourceConfig 定义 | `src/datasource/config.py` | dataclass: name/dialect/mode/host/port/database/username/password/engine/schema/description/tags/extra_params | 单测完成 | P0 |
| 2.1.2 | DataSourceRegistry | `src/datasource/registry.py` | register_provider() / resolve() / list_all() / _create_engine() | 单测完成 | P0 |
| 2.1.3 | DataSourceProvider 抽象基类 | `src/datasource/providers/base.py` | lookup() / extract_schema() / test_connection() 抽象方法 | 单测完成 | P0 |
| 2.1.4 | DataSourceConfigStore | `src/datasource/config_store.py` | 外挂模式配置持久化到 PostgreSQL | 待开发[^3] | P1 |
| 2.1.5 | DataSourceCreateRequest | `src/datasource/providers/external.py` | 外挂模式注册请求体 (临时，后续迁移到 api/schemas.py) | 开发完成 | P1 |

### 2.2 内置模式 Provider (Embedded)

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.2.1 | EmbeddedDataSourceProvider 类 | `src/datasource/providers/embedded.py` | DataSourceProvider 完整实现 | 单测完成 | P0 |
| 2.2.2 | _load_from_env() | 同上 | 从环境变量解析数据源 (单/多数据源) | 单测完成 | P0 |
| 2.2.3 | _is_django_project() | 同上 | 检测当前环境是否为 Django 项目 | 单测完成 | P1 |
| 2.2.4 | _from_django_config() | 同上 | 从 Django settings.DATABASES 解析 | 单测完成 | P1 |
| 2.2.5 | _has_sqlalchemy_engine() | 同上 | 检测是否存在 SQLAlchemy engine | 单测完成 | P1 |
| 2.2.6 | _from_sqlalchemy_engine() | 同上 | 从 SQLAlchemy engine 提取连接信息 | 单测完成 | P1 |
| 2.2.7 | _load_from_orm() | 同上 | ORM 自发现入口 (Django/SQLAlchemy 路由) | 单测完成 | P1 |
| 2.2.8 | _find_orm_models() | 同上 | 扫描 Django Model / SQLAlchemy declarative_base | 单测完成 | P1 |
| 2.2.9 | _extract_model_description() | 同上 | 从 Model.__doc__ / verbose_name 提取中文描述 | 单测完成 | P1 |
| 2.2.10 | extract_schema() — ORM 优先 | 同上 | ① ORM Model ② DB 内省兜底 | 单测完成 | P1 |

### 2.3 外挂模式 Provider (External)

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.3.1 | ExternalDataSourceProvider 类 | `src/datasource/providers/external.py` | DataSourceProvider 完整实现 | 单测完成 | P1 |
| 2.3.2 | register() | 同上 | 加密凭证 → 注册 → 后台预采集 Schema | 单测完成 | P1 |
| 2.3.3 | unregister() | 同上 | 移除数据源，关闭连接池 | 单测完成 | P1 |
| 2.3.4 | test_connection() | 同上 | SELECT 1 连通性测试 | 单测完成 | P1 |
| 2.3.5 | extract_schema() — 纯内省 | 同上 | DB 内省 + 手工标注补充 | 单测完成 | P1 |
| 2.3.6 | load_yaml() + from_yaml() | 同上 | 解析 config/datasources.yaml | 单测完成 | P1 |
| 2.3.7 | POST /datasources | `src/api/routes.py` | 注册数据源 | 单测完成 | P1 |
| 2.3.8 | DELETE /datasources/{name} | `src/api/routes.py` | 删除数据源 | 单测完成 | P1 |
| 2.3.9 | GET /datasources | `src/api/routes.py` | 列出数据源 (分页) | 单测完成 | P1 |

### 2.4 凭证管理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.4.1 | CredentialManager | `src/datasource/credential_manager.py` | encrypt() / decrypt() — AES-256 加密 | 单测完成 | P0 |
| 2.4.2 | 环境变量凭证引用 | 同上 | resolve_env_ref() — 解析 ${VAR_NAME} 占位符 | 单测完成 | P0 |
| 2.4.3 | KMS 集成 (远期) | 同上 | 对接 Vault / AWS KMS / Azure Key Vault | 待开发[^4] | P3 |

### 2.5 DB 内省

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.5.1 | introspect_columns() — ClickHouse | `src/datasource/introspection.py` | `system.columns` 查询 | 单测完成 | P0 |
| 2.5.2 | introspect_columns() — MySQL | 同上 | `INFORMATION_SCHEMA.COLUMNS` 查询 | 单测完成 | P0 |
| 2.5.3 | introspect_columns() — PostgreSQL | 同上 | `INFORMATION_SCHEMA.COLUMNS` + `pg_description` 查询 | 单测完成 | P0 |
| 2.5.4 | introspect_foreign_keys() — MySQL | 同上 | `KEY_COLUMN_USAGE` 查询外键 | 单测完成 | P0 |
| 2.5.5 | introspect_foreign_keys() — PostgreSQL | 同上 | `pg_constraint` 查询外键 | 单测完成 | P0 |
| 2.5.6 | introspect_foreign_keys() — ClickHouse | 同上 | ClickHouse 无外键，返回空列表 | 单测完成 | P0 |
| 2.5.7 | estimate_row_count() — ClickHouse | 同上 | `SELECT COUNT(*)` | 单测完成 | P0 |
| 2.5.8 | estimate_row_count() — MySQL | 同上 | `INFORMATION_SCHEMA.TABLES` 行数估算 | 单测完成 | P0 |
| 2.5.9 | estimate_row_count() — PostgreSQL | 同上 | `pg_class.reltuples` 估算 | 单测完成 | P0 |
| 2.5.10 | _query_metadata() 权限告警 | 同上 | INFORMATION_SCHEMA 权限不足时写入 SYSTEM_WARNING 类型的 KnowledgeEntry | 待开发 | P1 |

### 2.6 Schema 数据结构

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.6.1 | SchemaSnapshot 定义 | `src/datasource/schema_snapshot.py` | dataclass: tables / field_semantics / business_rules / sql_templates | 单测完成 | P0 |
| 2.6.2 | SchemaSnapshot.to_prompt_text() | 同上 | 格式化为 LLM Prompt 可用的 Markdown 表格文本 | 单测完成 | P0 |
| 2.6.3 | SchemaSnapshot.merge() | 同上 | 合并多个 SchemaSnapshot（ORM + 内省结果） | 单测完成 | P0 |
| 2.6.4 | TableSchema 定义 | 同上 | dataclass: name / description / columns / relations / row_count_estimate / partition_key / tags | 单测完成 | P0 |
| 2.6.5 | ColumnInfo 定义 | 同上 | dataclass: name / type / comment / is_nullable / is_primary_key / enum_values | 单测完成 | P0 |
| 2.6.6 | TableRelation 定义 | 同上 | dataclass: target_table / join_key / relation_type | 单测完成 | P0 |

---

## 3. 数据库连接器 (connectors/) `[P0:8 P1:2 P2:4]`

### 3.1 连接器基类

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.1.1 | ConnectorBase 抽象类 | `src/connectors/base.py` | execute/explain/health_check/close + rows_to_dict_list | 单测完成 | P0 |
| 3.1.2 | 连接池工厂 | 同上 | create_engine() + create_connector() — URL 构建 + SQLAlchemy AsyncEngine | 单测完成 | P0 |
| 3.1.3 | 查询超时控制 | 同上 | _get_timeout() — dialect 自适应超时 SQL | 单测完成 | P0 |
| 3.1.4 | 结果格式化 | 同上 | rows_to_dict_list() — RowMapping → list[dict] | 单测完成 | P0 |

### 3.2 ClickHouse 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.2.1 | ClickHouseConnector | `src/connectors/clickhouse.py` | 继承 ConnectorBase, clickhouse+asynch 驱动 | 单测完成 | P0 |
| 3.2.2 | execute() | 同上 | 继承自 ConnectorBase.execute() | 单测完成 | P0 |
| 3.2.3 | explain() | 同上 | EXPLAIN SYNTAX — 继承自基类 | 单测完成 | P0 |
| 3.2.4 | health_check() | 同上 | SELECT 1 — 继承自基类 | 单测完成 | P0 |
| 3.2.5 | get_partition_key() | 同上 | 查询 system.tables 分区键 | 单测完成 | P0 |

### 3.3 MySQL 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.3.1 | MySQLConnector | `src/connectors/mysql.py` | 继承 ConnectorBase, mysql+aiomysql 驱动 | 单测完成 | P2 |
| 3.3.2 | execute() | 同上 | 继承自基类 | 单测完成 | P2 |
| 3.3.3 | explain() | 同上 | EXPLAIN FORMAT=TREE — 继承自基类 | 单测完成 | P2 |
| 3.3.4 | health_check() | 同上 | 继承自基类 | 单测完成 | P2 |

### 3.4 PostgreSQL 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.4.1 | PostgreSQLConnector | `src/connectors/postgres.py` | 继承 ConnectorBase, postgresql+asyncpg 驱动 | 单测完成 | P2 |
| 3.4.2 | execute() | 同上 | 继承自基类 | 单测完成 | P2 |
| 3.4.3 | explain() | 同上 | EXPLAIN (ANALYZE false) — 继承自基类 | 单测完成 | P2 |
| 3.4.4 | health_check() | 同上 | 继承自基类 | 单测完成 | P2 |

---

## 4. LangGraph 编排引擎 (graph/) `[P0:28 P1:16 P2:3 P3:1]`

### 4.1 状态定义与工作流组装

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.1.1 | AnalysisState TypedDict | `src/graph/state.py` | 28 个字段的完整状态定义 | 单测完成 | P0 |
| 4.1.2 | StateGraph 组装 + compile | `src/graph/workflow.py` | 注册 10 个 Node + 9 条边 + 4 组条件路由 | 单测完成 | P0 |
| 4.1.3 | after_layer3() | 同上 | security_block→终止 / syntax_error→重试 / ok→下一步 | 单测完成 | P0 |
| 4.1.4 | after_layer4() | 同上 | 失败且<3次→重试 / ≥3次→放弃 / ok→执行 | 单测完成 | P0 |
| 4.1.5 | should_retry() | 同上 | 执行错误且<3→generate_sql / 否则→build_response | 单测完成 | P0 |
| 4.1.6 | route_by_intent() | 同上 | file_analysis→mcp_agent / 其他→retrieve_schema | 单测完成 | P0 |
| 4.1.7 | MCP Agent Node 扩展 | 同上 | 使用 create_react_agent 为文件分析场景创建动态工具调用 Node | 待开发 |

### 4.2 classify_intent Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.2.1 | classify_intent_node() | `src/graph/nodes/classify_intent.py` | 规则匹配 7 种意图 (Phase 2 切 LLM) | 单测完成 | P0 |
| 4.2.2 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别 Prompt 模板 | 单测完成 | P0 |
| 4.2.3 | Skill 匹配触发 | 同上 | 预留接口 (Phase 2 集成 SkillManager) | 开发完成 | P1 |
| 4.2.4 | 输出: intent / activated_skills / skill_prompt_override / skill_tools | 同上 | Skill 信息写入 state | 开发完成 | P0 |

### 4.3 retrieve_schema Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.3.1 | retrieve_schema_node() | `src/graph/nodes/retrieve_schema.py` | 从 injected schema 提取表结构 (Phase 2 向量检索) | 单测完成 | P0 |
| 4.3.2 | 关键词提取 + 向量检索 | 同上 | Phase 2: ChromaDB 语义检索 (依赖模块 6) | 待开发[^5] | P1 |
| 4.3.5 | 检索业务规则 | 同上 | Phase 2: BusinessRuleStore (依赖模块 6) | 待开发[^5] | P1 |
| 4.3.6 | 检索历史 SQL 模板 | 同上 | Phase 2: LongTermMemoryStore (依赖模块 7) | 待开发[^5] | P1 |

### 4.4 generate_sql Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.4.1 | generate_sql_node() | `src/graph/nodes/generate_sql.py` | 模板拼接 + 错误回注 (Phase 2 ChatOpenAI) | 单测完成 | P0 |
| 4.4.2 | SQL_GENERATION_SYSTEM_PROMPT | `src/llm/prompts.py` | SQL 生成 Prompt + 方言速查表 | 单测完成 | P0 |
| 4.4.3 | 方言 Prompt 注入 | 同上 | get_dialect_cheatsheet() — 3 种方言速查 | 单测完成 | P0 |
| 4.4.6 | 错误回注处理 | 同上 | retry_count>0 时返回修复占位 | 单测完成 | P0 |
| 4.4.10 | format_schema_for_prompt() | 同上 | 表结构 → Markdown 格式化 | 单测完成 | P0 |

### 4.5 layer3_validate Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.5.1 | layer3_validate_node() | `src/graph/nodes/layer3_validate.py` | sqlglot 语法 + 14 项安全拦截正则 | 单测完成 | P0 |
| 4.5.2 | SQL 安全拦截 | 同上 | 14 正则黑名单: INSERT/DELETE/DROP/ALTER/... | 单测完成 | P0 |
| 4.5.3 | sqlglot 语法解析校验 | 同上 | sqlglot.parse(sql, dialect) | 单测完成 | P0 |
| 4.5.7 | 输出: sql_valid / errors / transpiled | 同上 | | 单测完成 | P0 |

### 4.6 layer4_explain Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.6.1 | layer4_explain_node() | `src/graph/nodes/layer4_explain.py` | Phase 2 对接 Connector | 开发完成 | P1 |

### 4.7 execute_sql Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.7.1 | execute_sql_node() | `src/graph/nodes/execute_sql.py` | Phase 2 对接 Registry (Phase 1 mock) | 单测完成 | P0 |

### 4.8 analyze_result Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.8.1 | analyze_result_node() | `src/graph/nodes/analyze_result.py` | 分析引擎集成: 统计+趋势+异常+占比 | 单测完成 | P0 |
| 4.8.2 | DATA_ANALYSIS_PROMPT | `src/llm/prompts.py` | 数据分析 Prompt | 单测完成 | P0 |
| 4.8.3 | 描述性统计 | `src/tools/analyzer.py` | 均值/中位数/标准差/分位数/空值率 | 单测完成 | P0 |
| 4.8.4 | 趋势分析 | 同上 | 环比/方向/移动平均 | 单测完成 | P0 |
| 4.8.5 | 归因分析 | 同上 | Phase 2 LLM 归因 (数据统计已就绪) | 待开发[^7] | P1 |
| 4.8.6 | 异常检测 | 同上 | Z-Score + IQR 两种方法 | 单测完成 | P0 |
| 4.8.7 | 占比分析 | 同上 | 集中度/分类聚合 | 单测完成 | P0 |
| 4.8.9 | 输出: analysis_result | 同上 | summary+insights+chart+followups | 单测完成 | P0 |

### 4.9 generate_chart Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.9.1 | generate_chart_node() | `src/graph/nodes/generate_chart.py` | Phase 2 ECharts 生成 (Phase 1 占位) | 单测完成 | P1 |
| 4.9.4 | CHART_RECOMMEND_PROMPT | `src/llm/prompts.py` | 图表推荐 Prompt | 单测完成 | P0 |

### 4.10 build_response Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 4.10.1 | build_response_node() | `src/graph/nodes/build_response.py` | 组装 success/error 两种响应 | 单测完成 | P0 |
| 4.10.2 | 正常响应 | 同上 | user_query+sql+data+analysis+chart | 单测完成 | P0 |
| 4.10.3 | 错误响应 | 同上 | error_code + error_message | 单测完成 | P0 |

---

## 5. 工具层 (tools/) `[P0:11 P1:2]`

### 5.1 内置工具

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 5.1.1 | SchemaExplorerTool | `src/tools/schema_explorer.py` | 继承 BaseTool，封装 SchemaManager.get_or_fetch_schema() | 开发完成 |
| 5.1.2 | SQLGeneratorTool | `src/tools/sql_generator.py` | 继承 BaseTool，封装 SQL 生成逻辑 | 开发完成 |
| 5.1.3 | SQLglotValidatorTool | `src/tools/sqlglot_validator.py` | 继承 BaseTool，封装 validate_with_sqlglot() | 开发完成 |
| 5.1.4 | DBExecutorTool | `src/tools/db_executor.py` | 继承 BaseTool，封装 SQL 执行逻辑 | 开发完成 |
| 5.1.5 | DBExplainTool | `src/tools/db_executor.py` | 继承 BaseTool，封装 EXPLAIN 空跑逻辑 | 开发完成 |
| 5.1.6 | DataAnalyzerTool | `src/tools/data_analyzer.py` | 继承 BaseTool，封装数据分析逻辑 | 开发完成 |
| 5.1.7 | ChartGeneratorTool | `src/tools/chart_generator.py` | 继承 BaseTool，封装图表生成逻辑 | 开发完成 |

### 5.2 sqlglot 校验工具

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 5.2.1 | validate_with_sqlglot() | `src/tools/sqlglot_validator.py` | 核心校验函数: 语法解析 + 函数白名单 + 方言转译 | 开发完成 |
| 5.2.2 | SUPPORTED_DIALECTS 常量 | 同上 | 20+ 种 sqlglot 支持的方言集合 | 开发完成 |
| 5.2.3 | _get_dialect_functions() | 同上 | 获取指定方言的内置函数白名单 | 开发完成 |
| 5.2.4 | _is_universal_func() | 同上 | 跨数据库通用函数集合 (COUNT/SUM/AVG/COALESCE...) | 开发完成 |
| 5.2.5 | _suggest_correct_function() | 同上 | ClickHouse/PostgreSQL 函数映射建议表 | 开发完成 |

---

## 6. 知识库管理 (knowledge/) `[P0:14 P1:10 P2:4 P3:1]`

### 6.1 Schema 缓存管理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.1.1 | SchemaManager 类 | `src/knowledge/schema_manager.py` | 三级回退: ChromaDB 缓存 → 文档仓库 → DB 自动拉取 | 开发完成 |
| 6.1.2 | get_or_fetch_schema() | 同上 | Schema 获取的主入口，包装完整三级回退逻辑 | 开发完成 |
| 6.1.3 | _query_cache() | 同上 | 查 ChromaDB 缓存，检查 TTL 是否过期 | 开发完成 |
| 6.1.4 | _find_uncached() | 同上 | 找出哪些表尚未缓存或已过期 → 合并为 _any_expired() | 开发完成 |
| 6.1.5 | _load_from_docs() | 同上 | 从 docs/metrics/ 目录加载 Markdown 文档 → 通过 DocLoader 实现 | 开发完成 |
| 6.1.6 | _introspect_from_db() | 同上 | DB 系统表自动拉取，生成表级 + 字段级双粒度索引 | 开发完成 |
| 6.1.7 | _upsert_to_cache() | 同上 | KnowledgeEntry 写入 ChromaDB | 开发完成 |
| 6.1.8 | _build_snapshot() | 同上 | 将所有 KnowledgeEntry 组装为 SchemaSnapshot | 开发完成 |
| 6.1.9 | _format_table_summary() | 同上 | 格式化为表级描述文本 | 开发完成 |
| 6.1.10 | _format_column_detail() | 同上 | 格式化为字段级描述文本 | 开发完成 |
| 6.1.11 | _execute_metadata_query() | 同上 | 执行元数据查询 → 通过 introspect_database() 间接实现 | 开发完成 |

### 6.2 知识条目

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.2.1 | KnowledgeSource Enum | `src/knowledge/models.py` | MANUAL_DOC / ORM_MODEL / DB_COMMENT / AUTO_INTROSPECT / USER_CORRECTION / SYSTEM_WARNING | 开发完成 |
| 6.2.2 | KnowledgeEntry dataclass | 同上 | id / content / source / table_name / column_name / category / tags / created_at / ttl / metadata | 开发完成 |
| 6.2.3 | 知识优先级判断 | 同上 | MANUAL_DOC > USER_CORRECTION > ORM_MODEL > DB_COMMENT > AUTO_INTROSPECT | 开发完成 |


### 6.3 业务规则存储

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.3.1 | BusinessRuleStore 类 | `src/knowledge/business_rules.py` | 业务规则存储: metric / filter / enum / period | 开发完成 |
| 6.3.2 | initialize() | 同上 | 启动时扫描 docs/metrics/ 目录，通过 DocLoader 索引 | 开发完成 |
| 6.3.3 | search_business_rules() | 同上 | 向量检索 Top-K 条相关规则（过滤 category="business_rule"） | 开发完成 |
| 6.3.4 | _index_metric_doc() | → `doc_loader.py` | Markdown 解析/切片/写入 → DocLoader.scan_and_load() 实现 | 开发完成 |
| 6.3.5 | _split_by_headings() | → `doc_loader.py` | 按 Markdown 标题拆分 → DocLoader._split_by_headings() | 开发完成 |
| 6.3.6 | _extract_metric_tags() | → `doc_loader.py` | 标签提取 → YAML frontmatter tags 字段 | 开发完成 |

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
| 6.5.1 | auto_discover_enum_values() | `src/knowledge/enum_discovery.py` | 对低基数列采样枚举值 (SELECT DISTINCT ... LIMIT 50) | 开发完成 |
| 6.5.2 | is_low_cardinality_candidate() | 同上 | 唯一值 ≤ 20 → 可能是枚举 | 开发完成 |
| 6.5.3 | TTL 设置 | 同上 | 枚举值 TTL=1天 (比表结构变化快) | 开发完成 |

### 6.6 文档加载器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.6.1 | Markdown 文档扫描 | `src/knowledge/doc_loader.py` | 递归扫描 docs/metrics/ 目录下所有 *.md 文件 | 开发完成 |
| 6.6.2 | YAML frontmatter 解析 | 同上 | 解析 Markdown 文件中的 YAML 元数据 (tags / category / tables) | 开发完成 |
| 6.6.3 | 按标题切片 | 同上 | 按 ## 将文档拆分为独立索引单元 | 开发完成 |
| 6.6.4 | ChromaDB 批量写入 | 同上 | Document 列表写入 ChromaDB collection → 由 BusinessRuleStore._upsert_rules() | 开发完成 |

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
| 10.1.1 | ChatOpenAI 工厂 | `src/llm/client.py` | get_openai_llm() — ChatOpenAI 实例 | 单测完成 | P0 |
| 10.1.2 | ChatAnthropic 工厂 | 同上 | get_anthropic_llm() — ChatAnthropic 实例 | 单测完成 | P0 |
| 10.1.3 | LLM 路由器 | 同上 | get_llm() — provider 自动路由 | 单测完成 | P0 |
| 10.1.4 | cheap_llm 工厂 | 同上 | get_cheap_llm() — gpt-4o-mini | 单测完成 | P0 |
| 10.1.5 | is_llm_available() | 同上 | API Key 可用性检测 | 单测完成 | P0 |

### 10.2 Prompt 模板

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 10.2.1 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别 Prompt | 单测完成 | P0 |
| 10.2.2 | SQL_GENERATION_SYSTEM_PROMPT | 同上 | SQL 生成 Prompt + 方言速查 | 单测完成 | P0 |
| 10.2.3 | DATA_ANALYSIS_PROMPT | 同上 | 数据分析 Prompt | 单测完成 | P0 |
| 10.2.4 | CHART_RECOMMEND_PROMPT | 同上 | 图表推荐 Prompt | 单测完成 | P0 |
| 10.2.7 | get_dialect_cheatsheet() | 同上 | 3 种方言速查表 | 单测完成 | P0 |
| 10.2.8 | Prompt 版本号管理 | 同上 | Phase 3: LangSmith A/B 测试 | 待开发[^6] | P2 |

---

## 11. API 层 (api/)

### 11.1 核心接口

| # | 功能 | 文件 | 路由 | 描述 | 状态 |
|---|------|------|------|------|------|
| 11.1.1 | POST /chat | `src/api/routes.py` | `POST /api/v1/chat` | NL 查询 → 完整分析结果 | 单测完成 | P0 |
| 11.1.2 | POST /chat/stream | `src/api/routes.py` | `POST /api/v1/chat/stream` | SSE 流式 (astream_events) | 单测完成 | P0 |
| 11.1.3 | GET /schema/tables | `src/api/routes.py` | `GET /api/v1/schema/tables` | 表列表 + 分页 + 搜索 | 单测完成 | P0 |
| 11.1.4 | GET /schema/tables/{name} | `src/api/routes.py` | `GET /api/v1/schema/tables/{table_name}` | 指定表结构 | 单测完成 | P0 |
| 11.1.5 | POST /schema/refresh | `src/api/routes.py` | `POST /api/v1/schema/refresh` | 手动刷新 Schema | 单测完成 | P0 |
| 11.1.6 | GET /history | `src/api/routes.py` | `GET /api/v1/history` | Phase 2: 会话历史 | 待开发[^8] | P1 |
| 11.1.7 | POST /datasources | `src/api/routes.py` | 注册数据源 | 单测完成 (2.3.7) | P1 |
| 11.1.8 | DELETE /datasources/{name} | `src/api/routes.py` | 删除数据源 | 单测完成 (2.3.8) | P1 |
| 11.1.9 | GET /datasources | `src/api/routes.py` | 列出数据源 (分页) | 单测完成 (2.3.9) | P1 |
| 11.1.10 | GET /health | `src/api/routes.py` | 健康检查 | 单测完成 | P0 |
| 11.1.11 | PUT /schema/.../comment | `src/api/routes.py` | 手动标注字段 | 单测完成 | P1 |
| 11.1.12 | POST /mcp/{name}/reset | `src/api/routes.py` | Phase 2: MCP reset (依赖模块 8) | 待开发[^8] | P1 |
| 11.1.13 | GET /metrics | `src/api/routes.py` | Phase 2: 指标列表 (依赖模块 6) | 待开发[^8] | P2 |

### 11.2 分页增强

| # | 功能 | 文件 | 路由 | 描述 | 状态 |
|---|------|------|------|------|------|
| 11.2.1 | 表列表分页 | `src/api/routes.py` | ?page=1&page_size=20&search=xxx | 单测完成 | P1 |
| 11.2.2 | 会话历史分页 | `src/api/routes.py` | Phase 2: Checkpointer 查询 | 待开发[^8] | P1 |
| 11.2.3 | 数据源列表分页 | `src/api/routes.py` | ?page=1&page_size=20 | 单测完成 | P1 |

### 11.3 请求/响应 Schema (重新编号为 11.3)

### 11.2 请求/响应 Schema

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.2.1 | ChatRequest | `src/api/schemas.py` | query / session_id / datasource | 单测完成 | P0 |
| 11.2.2 | ChatResponse | 同上 | success / sql / data / analysis / chart | 单测完成 | P0 |
| 11.2.5 | DataSourceCreateRequest | 同上 | name / dialect / host / ... | 单测完成 | P0 |
| 11.2.7 | HealthResponse | 同上 | status / llm_available / uptime | 单测完成 | P0 |

### 11.3 流式输出

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 11.3.1 | stream_analysis() | `src/api/streaming.py` | FastAPI SSE endpoint: astream_events 循环推送 | 开发完成 |
| 11.3.2 | on_chat_model_stream 事件处理 | 同上 | LLM token 级别流式推送 | 开发完成 |
| 11.3.3 | on_chain_start 事件处理 | 同上 | Node 开始执行通知 | 开发完成 |
| 11.3.4 | on_chain_end 事件处理 | 同上 | Node 执行完成通知 (含 output) | 开发完成 |
| 11.3.5 | SSE 格式化 | 同上 | `data: {json}\n\n` 格式包装 | 开发完成 |

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
| 13.1 | compute_statistics() | `src/tools/analyzer.py` | 均值/中位数/标准差/分位数/空值率 | 单测完成 | P0 |
| 13.2 | compute_trend() | 同上 | 环比/方向/移动平均 | 单测完成 | P0 |
| 13.3 | detect_outliers_zscore() | 同上 | Z-Score 异常检测 | 单测完成 | P0 |
| 13.4 | detect_outliers_iqr() | 同上 | IQR 异常检测 | 单测完成 | P0 |
| 13.5 | compute_concentration() | 同上 | Top N 集中度 | 单测完成 | P0 |
| 13.6 | compute_correlation() | 同上 | Pearson 相关系数 | 单测完成 | P0 |

---

## 14. 可视化引擎 `[P1:6 P2:2]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 14.1 | 智能选图 classify_chart_type() | `src/tools/chart_generator.py` | 时间+数值→line / 分类+数值→bar / 占比→pie / 双数值→scatter / 通用→table | 开发完成 |
| 14.2 | ECharts 折线图生成 | 同上 | 生成 line chart 的 ECharts option JSON | 开发完成 |
| 14.3 | ECharts 柱状图生成 | 同上 | 生成 bar chart 的 ECharts option JSON | 开发完成 |
| 14.4 | ECharts 饼图生成 | 同上 | 生成 pie chart 的 ECharts option JSON | 开发完成 |
| 14.5 | ECharts 散点图生成 | 同上 | 生成 scatter chart 的 ECharts option JSON | 开发完成 |
| 14.6 | ECharts 热力图生成 | 同上 | 生成 heatmap chart 的 ECharts option JSON | 待开发 |
| 14.7 | 表格渲染 | 同上 | 生成 Markdown 表格或 HTML table | 开发完成 |
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

---

## 待开发项台账

每个待开发项均标注原因、触发条件和预计时机。条件满足时主动提醒开发。

[^1]: **1.2.3 MCP Server 注册表** — 原因: MCP Client 模块(8)未实现，配置文件结构需与 `MCPClientManager.connect_all()` 同步定义。条件: MCP Client Manager 开发时一并创建。时机: Phase 1 后续(模块 8)。

[^2]: **1.3.7 全局异常中间件 / 2.3.7~9 外挂 API 路由** — 原因: 依赖 FastAPI 路由体系，当前 `api/` 仅占位。条件: `api/routes.py` + `api/schemas.py` 创建时。时机: Phase 2(模块 11)。

[^3]: **2.1.4 DataSourceConfigStore** — 原因: PostgreSQL `datasource_configs` 表未创建，迁移文件(17.2.1)待开发。条件: `migrations/001_initial.py` 执行后。时机: Phase 3(模块 17)。

[^4]: **2.4.3 KMS 集成** — 原因: 需 Vault/AWS KMS/Azure Key Vault 等外部基础设施，当前 AES 本地加密已覆盖 MVP。条件: 生产环境 KMS 就绪。时机: Phase 4。

[^5]: **4.3.2/4.3.5/4.3.6 Schema 关键词+向量检索/业务规则/历史模板** — 原因: 依赖模块 6 (知识库 ChromaDB) 和模块 7 (长期记忆 Core) 未实现。条件: SchemaManager + BusinessRuleStore + LongTermMemoryStore 就绪。时机: Phase 2 (模块 6/7 完成后)。

[^6]: **10.2.8 Prompt 版本号管理** — 原因: 需 LangSmith A/B 测试基础设施 + CI 集成。当前 Phase 1 无此需求。条件: Phase 3 生产化评估。时机: Phase 3。

[^7]: **4.8.5 归因分析** — 原因: 归因分析需 LLM 理解业务语义（"为什么销售额下降"），纯统计只能提供维度下钻数据，无法生成归因解释。条件: LLM 客户端(10.1)就绪 + ChatOpenAI 可用。时机: Phase 2 (模块 10 完成后)。

[^8]: **11.1.6/11.1.12/11.1.13/11.2.2 会话历史/MCP重置/指标列表/历史分页** — 原因: 依赖 Checkpointer(模块7)/MCPClientManager(模块8)/BusinessRuleStore(模块6) 未实现。条件: 对应模块就绪后立即实现。时机: Phase 2。

