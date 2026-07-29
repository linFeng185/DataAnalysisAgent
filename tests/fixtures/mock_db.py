"""可复用的 SQLite 内存数据库和真实连接器 fixture。"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.connectors.sqlite import SQLiteConnector
from src.datasource.config import DataSourceConfig
from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema


@dataclass(slots=True)
class SQLiteMemoryDB:
    """持有共享内存 Engine、Connector 和已知 Schema。"""

    engine: AsyncEngine
    datasource: DataSourceConfig

    # 方法作用：创建固定订单样本的 SQLite 内存数据库。
    # Args: cls - SQLiteMemoryDB 类；name - 数据源名称。
    # Returns: 已建表、写入样本并绑定 Connector 的数据库对象。
    @classmethod
    async def create(cls, name: str = "sqlite-memory") -> "SQLiteMemoryDB":
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=sa.pool.StaticPool,
        )
        async with engine.begin() as connection:
            await connection.execute(sa.text(
                "CREATE TABLE orders ("
                "id INTEGER PRIMARY KEY, customer TEXT NOT NULL, amount REAL NOT NULL)"
            ))
            await connection.execute(sa.text(
                "INSERT INTO orders (id, customer, amount) VALUES "
                "(1, '张三', 120.5), (2, '李四', 80.0), (3, '张三', 99.5)"
            ))
        schema = SchemaSnapshot(tables=[TableSchema(
            name="orders",
            description="订单测试表",
            columns=[
                ColumnInfo("id", "INTEGER", "订单编号", is_nullable=False, is_primary_key=True),
                ColumnInfo("customer", "TEXT", "客户名称", is_nullable=False),
                ColumnInfo("amount", "REAL", "订单金额", is_nullable=False),
            ],
            row_count_estimate=3,
        )])
        datasource = DataSourceConfig(
            name=name,
            dialect="sqlite",
            mode="embedded",
            database=":memory:",
            engine=engine,
            schema=schema,
        )
        datasource.connector = SQLiteConnector(datasource).attach_engine(engine)
        return cls(engine=engine, datasource=datasource)

    # 方法作用：执行只读 SQL 并返回普通字典行。
    # Args: self - 当前数据库；sql - 待执行 SQL。
    # Returns: 查询结果字典列表。
    async def fetch_all(self, sql: str) -> list[dict]:
        async with self.engine.connect() as connection:
            result = await connection.execute(sa.text(sql))
            return [dict(row._mapping) for row in result]

    # 方法作用：释放 SQLite 异步 Engine。
    # Args: self - 当前数据库。
    # Returns: 无返回值。
    async def close(self) -> None:
        await self.engine.dispose()
