"""连接器基类 — 统一各数据库的 execute/explain/health_check 接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.config import get_settings
from src.datasource.config import DataSourceConfig
from src.logging_config import get_logger

logger = get_logger(__name__)

EXPLAIN_TEMPLATES = {
    "clickhouse": "EXPLAIN SYNTAX {sql}",
    "mysql":      "EXPLAIN FORMAT=TREE {sql}",
    "postgres":   "EXPLAIN (ANALYZE false) {sql}",
    "presto":     "EXPLAIN (TYPE VALIDATE) {sql}",
    "hive":       "EXPLAIN {sql}",
}


class ConnectorBase(ABC):
    """数据库连接器抽象基类。"""

    def __init__(self, config: DataSourceConfig) -> None:
        self.config = config
        self._engine: AsyncEngine | None = None

    async def create_engine(self) -> AsyncEngine:
        """3.1.2 创建 SQLAlchemy AsyncEngine + 连接池。"""
        settings = get_settings()
        url = self._build_url()
        self._engine = create_async_engine(
            url,
            pool_size=self.config.extra_params.get("pool_size", 5),
            max_overflow=self.config.extra_params.get("max_overflow", 10),
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=settings.env == "dev",
        )
        return self._engine

    @abstractmethod
    def _build_url(self) -> str:
        """3.1.2 构建方言特定的连接 URL。"""

    @property
    def engine(self) -> AsyncEngine | None:
        return self._engine

    async def execute(self, sql: str, params: dict | None = None) -> list[dict]:
        """3.1.1 执行 SQL，返回 list[dict]."""
        if not self._engine:
            await self.create_engine()
        async with self._engine.connect() as conn:
            # 3.1.3 超时控制
            if timeout_sql := self._get_timeout():
                await conn.execute(__import__("sqlalchemy").text(timeout_sql))
            import sqlalchemy as sa
            result = await conn.execute(sa.text(sql), params or {})
            return self.rows_to_dict_list(result)

    async def explain(self, sql: str) -> dict:
        """3.1.1 EXPLAIN 空跑校验。"""
        settings = get_settings()
        if self.config.dialect in settings.explain_skip_dialects:
            return {"valid": True, "errors": []}
        template = EXPLAIN_TEMPLATES.get(self.config.dialect)
        if not template:
            return {"valid": True, "errors": []}
        try:
            await self.execute(template.format(sql=sql))
            return {"valid": True, "errors": []}
        except Exception as e:
            return {"valid": False, "errors": [
                {"type": "semantic_error", "message": str(e).split("Stack trace:")[0][:500]}
            ]}

    async def health_check(self) -> bool:
        """3.1.1 连通性检查。"""
        try:
            await self.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """3.1.1 关闭连接池。"""
        if self._engine:
            await self._engine.dispose()
            self._engine = None

    @staticmethod
    def rows_to_dict_list(rows) -> list[dict]:
        """3.1.4 SQLAlchemy 结果行 → list[dict]."""
        return [dict(row._mapping) for row in rows]

    def _get_timeout(self) -> str | None:
        """3.1.3 方言超时设置。"""
        settings = get_settings()
        s = settings.max_execution_time
        return {
            "clickhouse": f"SET max_execution_time = {s}",
            "mysql":      f"SET SESSION max_execution_time = {s * 1000}",
            "postgres":   f"SET statement_timeout = '{s * 1000}ms'",
        }.get(self.config.dialect)


def create_connector(ds: DataSourceConfig) -> ConnectorBase:
    """连接器工厂。"""
    if ds.dialect == "clickhouse":
        from src.connectors.clickhouse import ClickHouseConnector
        return ClickHouseConnector(ds)
    if ds.dialect == "mysql":
        from src.connectors.mysql import MySQLConnector
        return MySQLConnector(ds)
    if ds.dialect == "postgres":
        from src.connectors.postgres import PostgreSQLConnector
        return PostgreSQLConnector(ds)
    if ds.dialect == "sqlite":
        from src.connectors.sqlite import SQLiteConnector
        return SQLiteConnector(ds)
    if ds.dialect == "oracle":
        from src.connectors.oracle import OracleConnector
        return OracleConnector(ds)
    if ds.dialect == "mssql":
        from src.connectors.mssql import SQLServerConnector
        return SQLServerConnector(ds)
    raise ValueError(f"不支持的方言: {ds.dialect}")
