# 3. 数据库连接器

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
