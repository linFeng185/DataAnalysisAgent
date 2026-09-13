# 2. 数据源管理

## 2. 数据源管理模块 (datasource/) `[P0:20 P1:12 P2:3 P3:1]`

### 2.1 配置对象与注册

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.1.1 | DataSourceConfig 定义 | `src/datasource/config.py` | dataclass: name/dialect/mode/host/port/database/username/password/engine/schema/description/tags/extra_params | 单测完成 | P0 |
| 2.1.2 | DataSourceRegistry | `src/datasource/registry.py` | register_provider() / resolve() / list_all()；旧缓存键复核租户归属，后台任务可枚举有数据源的租户 | 单测完成 | P0 |
| 2.1.3 | DataSourceProvider 抽象基类 | `src/datasource/providers/base.py` | lookup() / extract_schema() / test_connection() 抽象方法 | 单测完成 | P0 |
| 2.1.4 | DataSourceConfigStore | `src/datasource/providers/external.py` | 页面数据源加密持久化到 PostgreSQL 并在启动时恢复，见 21.3.4 | 单测完成 | P1 |
| 2.1.5 | DataSourceCreateRequest | `src/datasource/providers/external.py` | 外挂模式注册请求体 (临时，后续迁移到 api/schemas.py) | 开发完成 | P1 |

### 2.2 内置模式 Provider (Embedded)

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.2.1 | EmbeddedDataSourceProvider 类 | `src/datasource/providers/embedded.py` | DataSourceProvider 完整实现 | 单测完成 | P0 |
| 2.2.2 | 历史环境变量发现入口退役 | 同上 | 已删除 `_load_from_env()`，旧单/多数据源变量不再注册连接；→ 见 25.8 | 单测完成 | P0 |
| 2.2.3 | _is_django_project() | 同上 | 检测当前环境是否为 Django 项目 | 单测完成 | P1 |
| 2.2.4 | _from_django_config() | 同上 | 从 Django settings.DATABASES 解析 | 单测完成 | P1 |
| 2.2.5 | _has_sqlalchemy_engine() | 同上 | 检测是否存在 SQLAlchemy engine | 单测完成 | P1 |
| 2.2.6 | _from_sqlalchemy_engine() | 同上 | 从 SQLAlchemy engine 提取连接信息 | 单测完成 | P1 |
| 2.2.7 | _load_from_orm() | 同上 | 保留 ORM 提取辅助方法，但构造/启动不再自动发现数据源；→ 见 25.8 | 单测完成 | P1 |
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
| 2.3.6 | load_persisted() 应用配置恢复 | 同上 | 已删除 YAML 加载入口，仅恢复 `datasource_configs`；→ 见 25.8 | 单测完成 | P1 |
| 2.3.7 | POST /datasources | `src/api/routes.py` | 注册数据源 | 单测完成 | P1 |
| 2.3.8 | DELETE /datasources/{name} | `src/api/routes.py` | 删除数据源 | 单测完成 | P1 |
| 2.3.9 | GET /datasources | `src/api/routes.py` | 列出数据源 (分页) | 单测完成 | P1 |
| 2.3.10 | Oracle 连通性探针 | `src/datasource/providers/external.py` | Oracle 21c 使用 DUAL 探针并记录失败原因，避免错误回退为 404 | 单测完成 | P1 |

### 2.4 凭证管理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 2.4.1 | CredentialManager | `src/datasource/credential_manager.py` | 每次加密生成随机 salt，保存 `v2:salt:token` 并兼容历史固定 salt token | 单测完成 | P0 |
| 2.4.2 | 环境变量凭证引用 | 同上 | resolve_env_ref() — 解析 ${VAR_NAME} 占位符 | 单测完成 | P0 |
| 2.4.3 | 密钥托管与轮换 | `src/datasource/secret_provider.py`、`rotate_credentials.py` | Vault KV v2/只读文件注入；版本化 Fernet；离线事务轮换两个凭证表；不是不可导出 HSM 模式 | 单测完成 | P3 |

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
| 2.5.10 | _query_metadata() 权限告警 | 同上 | 归一化各方言元数据权限错误，在 SchemaManager 边界写入租户级 SYSTEM_WARNING 并累计 Prometheus 指标 | 单测完成 | P1 |
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

### 2026-09-13 连接资源释放回归

- DataSourceRegistry 注册 AppContext 异步关闭器，关闭时清理缓存、复用原连接释放路径并等待失效释放任务；重复关闭幂等。`tests/test_datasource/test_registry_shutdown.py` 验证活动连接与待释放连接都被等待。
- 真实深度报表此前在进程结束时报 aiomysql `Event loop is closed`，修复后第八轮完整生成及关闭日志没有该异常。详细验收见 `features/23-report-workbench.md`；不涉及凭证轮换或 Vault 能力。

### 模块收尾

模块功能点共 47 项，已完成 47 项，待开发 0 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 无 | 本模块代码功能均已实现 | Vault 实例和真实 PG 多租户轮换仍需部署验收 | 生产上线前 |

2026-09-06：2.4.3 经开发完成后通过 41 条凭证相关测试（MockTransport、文件读取、
启动注入、版本兼容、轮换回滚与幂等）。实现及配置见 `spec/31-secret-custody.md`。
未连接真实 Vault，未对用户现有数据库执行轮换；不把外部环境验收计为完成。

---
