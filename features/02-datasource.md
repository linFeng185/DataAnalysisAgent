# 2. 数据源管理

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
| 2.3.2 | register() | 同上 | 加密凭证 → 注册到全局 Provider，Schema 在 resolve/refresh 时加载 | 单测完成 | P1 |
| 2.3.3 | unregister() | 同上 | 移除数据源，关闭连接池 | 单测完成 | P1 |
| 2.3.4 | test_connection() | 同上 | 按方言选择连通性探针，Oracle 使用 SELECT 1 FROM DUAL | 单测完成 | P1 |
| 2.3.5 | extract_schema() — 纯内省 | 同上 | DB 内省 + 手工标注补充 | 单测完成 | P1 |
| 2.3.6 | load_yaml() + from_yaml() | 同上 | 解析 config/datasources.yaml | 单测完成 | P1 |
| 2.3.7 | POST /datasources | `src/api/routes.py` | 注册数据源 | 单测完成 | P1 |
| 2.3.8 | DELETE /datasources/{name} | `src/api/routes.py` | 删除数据源 | 单测完成 | P1 |
| 2.3.9 | GET /datasources | `src/api/routes.py` | 列出数据源 (分页) | 单测完成 | P1 |
| 2.3.10 | Oracle 连通性探针 | `src/datasource/providers/external.py` | Oracle 21c 使用 DUAL 探针并记录失败原因，避免错误回退为 404 | 单测完成 | P1 |

### 2.4 凭证管理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.4.1 | CredentialManager | `src/datasource/credential_manager.py` | 每次加密生成随机 salt，保存 `v2:salt:token` 并兼容历史固定 salt token | 单测完成 | P0 |
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
| 2.5.11 | Oracle Schema 内省 | `src/datasource/introspection.py` | 使用 `ALL_TABLES`、`ALL_TAB_COLUMNS` 和当前 schema 查询表、字段、外键及行数 | 单测完成 | P1 |

### 2.6 Schema 数据结构

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.6.1 | SchemaSnapshot 定义 | `src/datasource/schema_snapshot.py` | dataclass: tables / field_semantics / business_rules / sql_templates | 单测完成 | P0 |
| 2.6.2 | SchemaSnapshot.to_prompt_text() | 同上 | 格式化为 LLM Prompt 可用的 Markdown 表格文本 | 单测完成 | P0 |
| 2.6.3 | SchemaSnapshot.merge() | 同上 | 合并多个 SchemaSnapshot（ORM + 内省结果） | 单测完成 | P0 |
| 2.6.4 | TableSchema 定义 | 同上 | dataclass: name / description / columns / relations / row_count_estimate / partition_key / tags | 单测完成 | P0 |
| 2.6.5 | ColumnInfo 定义 | 同上 | dataclass: name / type / comment / is_nullable / is_primary_key / enum_values | 单测完成 | P0 |
| 2.6.6 | TableRelation 定义 | 同上 | dataclass: target_table / join_key / relation_type | 单测完成 | P0 |

### 模块收尾

模块功能点共 47 项，已完成 44 项，待开发 3 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 2.1.4 DataSourceConfigStore | 动态数据源当前驻留全局 Provider，持久化表和加密字段尚未落地 | 完成 17.1.5 数据源配置表及租户唯一键设计 | Phase 3，多实例部署前 |
| 2.4.3 KMS 集成 | 当前 PBKDF2 + Fernet 已满足本地部署，外部 Vault/KMS 需要额外运维依赖 | 确定生产密钥托管平台和轮换流程 | Phase 3，生产密钥治理批次 |
| 2.5.10 _query_metadata() 权限告警 | 当前只返回可用元数据，权限不足告警需要统一 SYSTEM_WARNING 知识条目契约 | 固化各方言权限错误映射并接入知识库告警 | Phase 2，Schema 权限治理批次 |

---
