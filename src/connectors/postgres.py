"""PostgreSQL 连接器 — 3.4.1~4。"""

from __future__ import annotations

from sqlalchemy import URL

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

    def _build_url(self) -> URL:
        """构建密码安全且日志默认脱敏的 PostgreSQL URL。

        Args:
            无，使用当前数据源配置。

        Returns:
            SQLAlchemy URL 对象。
        """
        c = self.config
        logger.debug("PostgreSQL URL 构建入口", datasource=c.name)
        result = URL.create(
            "postgresql+asyncpg",
            username=c.username or None,
            password=c.password or None,
            host=c.host or None,
            port=c.port or None,
            database=c.database or None,
        )
        logger.info("PostgreSQL URL 构建完成", datasource=c.name)
        return result
