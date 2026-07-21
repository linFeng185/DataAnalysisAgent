"""SQL Server 连接器，通过线程池适配同步 pymssql 驱动。"""

from __future__ import annotations

import asyncio
from urllib.parse import quote_plus

import sqlalchemy as sa

from src.connectors.base import ConnectorBase
from src.connectors.registry import register_connector
from src.logging_config import get_logger


logger = get_logger(__name__)


@register_connector("mssql")
class SQLServerConnector(ConnectorBase):
    """SQL Server 同步连接器和 SHOWPLAN 语义校验实现。"""

    explain_template = "{sql}"

    # 方法作用：构建 pymssql SQLAlchemy URL。
    # Args: 无，使用当前数据源配置。
    # Returns: SQL Server 连接 URL。
    def _build_url(self) -> str:
        """对密码进行 URL 编码后生成连接串。"""
        logger.debug("SQL Server URL 构建入口", datasource=self.config.name)
        password = quote_plus(self.config.password) if self.config.password else ""
        result = (
            f"mssql+pymssql://{self.config.username}:{password}"
            f"@{self.config.host}:{self.config.port}/{self.config.database}"
        )
        logger.info("SQL Server URL 构建完成", datasource=self.config.name)
        return result

    # 方法作用：创建同步 SQL Server Engine。
    # Args: 无，使用当前数据源配置。
    # Returns: SQLAlchemy 同步 Engine。
    async def create_engine(self):
        """在线程池外只创建轻量 Engine，真实连接由首次查询建立。"""
        logger.debug("SQL Server 引擎创建入口", datasource=self.config.name)
        try:
            self._engine = sa.create_engine(
                self._build_url(),
                pool_size=2,
                max_overflow=5,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
        except Exception as exc:
            logger.error("SQL Server 引擎创建失败", error=str(exc), exc_info=True)
            raise
        logger.info("SQL Server 引擎创建完成", datasource=self.config.name)
        return self._engine

    # 方法作用：在同一连接中启用 SHOWPLAN、执行 SQL 并可靠关闭开关。
    # Args: sql - 待校验只读 SQL。
    # Returns: valid 和 errors 字段。
    async def explain(self, sql: str) -> dict:
        """遵循 SQL Server 要求分批执行 SHOWPLAN 语句。"""
        logger.debug("SQL Server EXPLAIN 入口", datasource=self.config.name, sql=sql)
        if self._engine is None:
            await self.create_engine()
        try:
            await asyncio.to_thread(self._execute_showplan_sync, sql)
        except Exception as exc:
            logger.error("SQL Server EXPLAIN 失败", error=str(exc), exc_info=True)
            return {
                "valid": False,
                "errors": [{"type": "semantic_error", "message": str(exc)[:500]}],
            }
        logger.info("SQL Server EXPLAIN 完成", datasource=self.config.name, valid=True)
        return {"valid": True, "errors": []}

    # 方法作用：同步执行 SHOWPLAN 三段语句并确保 OFF。
    # Args: sql - 待校验只读 SQL。
    # Returns: 无返回值，数据库异常原样抛出。
    def _execute_showplan_sync(self, sql: str) -> None:
        """同一连接中的 ON/OFF 不能合并为一个批次。"""
        logger.debug("SQL Server SHOWPLAN 同步执行入口", datasource=self.config.name, sql=sql)
        with self._engine.connect() as connection:
            connection.execute(sa.text("SET SHOWPLAN_TEXT ON"))
            try:
                connection.execute(sa.text(sql))
            finally:
                connection.execute(sa.text("SET SHOWPLAN_TEXT OFF"))
        logger.info("SQL Server SHOWPLAN 同步执行完成", datasource=self.config.name)
