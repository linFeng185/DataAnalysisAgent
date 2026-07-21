"""PostgreSQL 连接器 — 3.4.1~4。"""

from __future__ import annotations

from src.connectors.base import ConnectorBase
from src.connectors.registry import register_connector
from src.config import get_settings
from src.logging_config import get_logger


logger = get_logger(__name__)


@register_connector("postgres")
class PostgreSQLConnector(ConnectorBase):
    """PostgreSQL 异步连接器 (postgresql+asyncpg)。"""

    explain_template = "EXPLAIN (ANALYZE false) {sql}"

    @property
    def timeout_sql(self) -> str:
        """返回 PostgreSQL statement_timeout SQL。

        Args:
            无。

        Returns:
            SET statement_timeout SQL。
        """
        logger.debug("PostgreSQL 超时 SQL 入口", datasource=self.config.name)
        result = f"SET statement_timeout = '{get_settings().max_execution_time * 1000}ms'"
        logger.info("PostgreSQL 超时 SQL 完成", datasource=self.config.name)
        return result

    def _build_url(self) -> str:
        c = self.config
        return (
            f"postgresql+asyncpg://{c.username}:{c.password}"
            f"@{c.host}:{c.port}/{c.database}"
        )
