"""MySQL 连接器 — 3.3.1~4。"""

from __future__ import annotations

from src.connectors.base import ConnectorBase


class MySQLConnector(ConnectorBase):
    """MySQL 异步连接器 (mysql+aiomysql)。"""

    def _build_url(self) -> str:
        c = self.config
        return (
            f"mysql+aiomysql://{c.username}:{c.password}"
            f"@{c.host}:{c.port}/{c.database}?charset=utf8mb4"
        )
