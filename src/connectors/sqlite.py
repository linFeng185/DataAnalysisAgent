"""SQLite 连接器 — 开发/演示用。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.connectors.base import ConnectorBase
from src.connectors.registry import register_connector
from src.logging_config import get_logger


logger = get_logger(__name__)


@register_connector("sqlite")
class SQLiteConnector(ConnectorBase):
    """SQLite 异步连接器 (aiosqlite)。不设连接池参数 (SQLite 不支持)。"""

    explain_template = "EXPLAIN QUERY PLAN {sql}"

    # 方法作用：根据 db_path 或 database 构建 SQLite 异步 URL。
    # Args: self - 当前 SQLiteConnector。
    # Returns: sqlite+aiosqlite URL。
    def _build_url(self) -> str:
        logger.debug("SQLite URL 构建入口", datasource=self.config.name)
        path = self.config.extra_params.get("db_path") or self.config.database or ":memory:"
        result = f"sqlite+aiosqlite:///{path}"
        logger.info("SQLite URL 构建完成", datasource=self.config.name)
        return result

    # 方法作用：声明 SQLite 不注入会话超时 SQL。
    # Args: self - 当前 SQLiteConnector。
    # Returns: None。
    def _get_timeout(self) -> str | None:
        logger.debug("SQLite 超时配置入口", datasource=self.config.name)
        logger.info("SQLite 超时配置完成", configured=False)
        return None

    # 方法作用：创建使用 StaticPool 的 SQLite 异步 Engine。
    # Args: self - 当前 SQLiteConnector。
    # Returns: 已缓存的 AsyncEngine。
    async def create_engine(self) -> AsyncEngine:
        """覆盖基类: SQLite 不需要 pool_size/max_overflow。"""
        import sqlalchemy as sa
        from src.config import get_settings
        logger.debug("SQLite 引擎创建入口", datasource=self.config.name)
        try:
            self._engine = create_async_engine(
                self._build_url(),
                poolclass=sa.pool.StaticPool,
                echo=get_settings().env == "dev",
            )
        except Exception as exc:
            logger.error(
                "SQLite 引擎创建失败",
                datasource=self.config.name,
                error=str(exc),
                exc_info=True,
            )
            raise
        logger.info("SQLite 引擎创建完成", datasource=self.config.name)
        return self._engine
