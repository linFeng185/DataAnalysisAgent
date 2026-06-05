"""ClickHouse 连接器 — 3.2.1~5。"""

from __future__ import annotations

from src.connectors.base import ConnectorBase


class ClickHouseConnector(ConnectorBase):
    """ClickHouse 异步连接器 (clickhouse+asynch)。"""

    def _build_url(self) -> str:
        c = self.config
        return (
            f"clickhouse+asynch://{c.username}:{c.password}"
            f"@{c.host}:{c.port}/{c.database}"
        )

    async def get_partition_key(self, table: str) -> str:
        """3.2.5 获取 ClickHouse 表分区键。"""
        try:
            rows = await self.execute(
                "SELECT partition_key FROM system.tables "
                "WHERE database = :db AND name = :table",
                {"db": self.config.database, "table": table},
            )
            return rows[0].get("partition_key", "") if rows else ""
        except Exception:
            return ""
