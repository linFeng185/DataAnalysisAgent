"""MySQL 连接器 — 3.3.1~4。"""

from __future__ import annotations

from sqlalchemy import URL

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

    def _build_url(self) -> URL:
        """构建密码安全且日志默认脱敏的 MySQL URL。

        Args:
            无，使用当前数据源配置。

        Returns:
            SQLAlchemy URL 对象。
        """
        c = self.config
        logger.debug("MySQL URL 构建入口", datasource=c.name)
        result = URL.create(
            "mysql+aiomysql",
            username=c.username or None,
            password=c.password or None,
            host=c.host or None,
            port=c.port or None,
            database=c.database or None,
            query={"charset": "utf8mb4"},
        )
        logger.info("MySQL URL 构建完成", datasource=c.name)
        return result
