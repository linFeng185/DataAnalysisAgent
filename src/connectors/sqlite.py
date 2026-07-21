"""SQLite 连接器 — 开发/演示用。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.connectors.base import ConnectorBase
from src.connectors.registry import register_connector


@register_connector("sqlite")
class SQLiteConnector(ConnectorBase):
    """SQLite 异步连接器 (aiosqlite)。不设连接池参数 (SQLite 不支持)。"""

    explain_template = "EXPLAIN QUERY PLAN {sql}"

    def _build_url(self) -> str:
        path = self.config.extra_params.get("db_path") or self.config.database or ":memory:"
        return f"sqlite+aiosqlite:///{path}"

    def _get_timeout(self) -> str | None:
        return None

    async def create_engine(self) -> AsyncEngine:
        """覆盖基类: SQLite 不需要 pool_size/max_overflow。"""
        import sqlalchemy as sa
        from src.config import get_settings
        self._engine = create_async_engine(
            self._build_url(),
            poolclass=sa.pool.StaticPool,
            echo=get_settings().env == "dev",
        )
        return self._engine
