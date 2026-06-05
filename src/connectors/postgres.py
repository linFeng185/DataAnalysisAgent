"""PostgreSQL 连接器 — 3.4.1~4。"""

from __future__ import annotations

from src.connectors.base import ConnectorBase


class PostgreSQLConnector(ConnectorBase):
    """PostgreSQL 异步连接器 (postgresql+asyncpg)。"""

    def _build_url(self) -> str:
        c = self.config
        return (
            f"postgresql+asyncpg://{c.username}:{c.password}"
            f"@{c.host}:{c.port}/{c.database}"
        )
