# 3. 数据库连接器

## 3. 数据库连接器 (connectors/) `[P0:8 P1:2 P2:4]`

### 3.1 连接器基类

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.1.1 | ConnectorBase 抽象类 | `src/connectors/base.py` | execute/explain/health_check/close + rows_to_dict_list | 单测完成 | P0 |
| 3.1.2 | 连接池工厂 | 同上 | `URL.create()` 安全保存特殊字符密码且默认脱敏 + SQLAlchemy AsyncEngine | 单测完成 | P0 |
| 3.1.3 | 查询超时与扫描预算 | 同上 + `src/security/sql_execution.py` | asyncio 总超时、方言/驱动超时、EXPLAIN 扫描门禁和统一结果上限 | 单测完成 | P0 |
| 3.1.4 | 结果格式化 | 同上 | rows_to_dict_list() — RowMapping → list[dict] | 单测完成 | P0 |

### 3.2 ClickHouse 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.2.1 | ClickHouseConnector | `src/connectors/clickhouse.py` | 继承 ConnectorBase，使用已声明的 clickhouse-connect HTTP 客户端 | 单测完成 | P0 |
| 3.2.2 | execute() | 同上 | query_rows_stream() 最多消费 max_rows+1，服务端同步下发扫描、结果和执行时间上限 | 单测完成 | P0 |
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

### 3.5 Oracle 连接器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 3.5.1 | OracleConnector | `src/connectors/oracle.py` | oracledb 驱动，同步 Engine + 线程池异步适配 | 单测完成 | P1 |
| 3.5.2 | service_name URL | 同上 | 使用 `?service_name=` 连接 Oracle XE PDB | 单测完成 | P1 |
| 3.5.3 | execute() | 同上 | 首次执行可 await 创建引擎，结果统一为 list[dict] | 单测完成 | P1 |
| 3.5.4 | health_check() | 同上 | 使用 SELECT 1 FROM DUAL | 单测完成 | P1 |
| 3.5.5 | close() | 同上 | 在线程池释放同步连接池 | 单测完成 | P1 |
| 3.5.6 | 驱动级执行超时 | `src/connectors/oracle.py`、`src/connectors/mssql.py` | Oracle call_timeout 与 SQL Server 查询超时纳入统一执行预算 | 单测完成 | P0 |

### 2026-09-13 ClickHouse 并发回归

- 深度报表共用只读客户端时，自动 session ID 导致并发查询报 `Attempt to execute concurrent queries within the same session`。客户端显式传入 `autogenerate_session_id=False`，不改变出站验证、只读和执行上限。
- 参数契约单测与完整真实报表重跑通过，最后一轮数据库执行无 ERROR。详见 `features/23-report-workbench.md`；不是通过串行化所有查询或掩盖连接器异常规避。

### 模块收尾

模块功能点共 23 项，已完成 23 项，待开发 0 项。

---
