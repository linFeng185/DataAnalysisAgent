"""MySQL 连接器 — 3.3.1~4。"""

from __future__ import annotations

from src.connectors.base import ConnectorBase
from src.connectors.registry import register_connector
from src.config import get_settings
from src.logging_config import get_logger


logger = get_logger(__name__)


@register_connector("mysql")
class MySQLConnector(ConnectorBase):
    """MySQL 异步连接器 (mysql+aiomysql)。"""

    explain_template = "EXPLAIN FORMAT=TREE {sql}"

    @property
    def timeout_sql(self) -> str:
        """返回 MySQL 毫秒级会话超时 SQL。

        Args:
            无。

        Returns:
            SET SESSION max_execution_time SQL。
        """
        logger.debug("MySQL 超时 SQL 入口", datasource=self.config.name)
        result = f"SET SESSION max_execution_time = {get_settings().max_execution_time * 1000}"
        logger.info("MySQL 超时 SQL 完成", datasource=self.config.name)
        return result

    def _build_url(self) -> str:
        c = self.config
        return (
            f"mysql+aiomysql://{c.username}:{c.password}"
            f"@{c.host}:{c.port}/{c.database}?charset=utf8mb4"
        )
