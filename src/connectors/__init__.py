"""
数据库连接器 — 各方言 SQL 执行细节。

业务职责：
  封装不同数据库方言的连接 URL、超时设置、EXPLAIN 语法差异。
  通过 ConnectorBase 抽象基类 + create_connector() 工厂函数实现多方言支持。

当前支持的方言：
  - ClickHouse  （clickhouse+asynch）
  - MySQL       （mysql+aiomysql）
  - PostgreSQL  （postgresql+asyncpg）
  - SQLite      （aiosqlite，演示/开发用）

新增方言：继承 ConnectorBase，重写 _build_url() 和 _get_timeout()
"""
