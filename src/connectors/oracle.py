"""Oracle 连接器 -- oracledb 驱动，通过线程池适配异步。"""
from __future__ import annotations

import asyncio
from typing import Any

import sqlalchemy as sa

from src.connectors.base import ConnectorBase
from src.logging_config import get_logger

logger = get_logger(__name__)


class OracleConnector(ConnectorBase):
    """使用同步 SQLAlchemy Engine 并在线程池中执行 Oracle 操作。"""

    def _build_url(self) -> str:
        """构建 Oracle service name 连接 URL。

        Args:
            无，使用构造时保存的数据源配置。

        Returns:
            包含 service_name 参数的 SQLAlchemy URL。
        """
        cfg = self.config
        from urllib.parse import quote_plus
        pwd = quote_plus(cfg.password) if cfg.password else ""
        return f"oracle+oracledb://{cfg.username}:{pwd}@{cfg.host}:{cfg.port}/?service_name={cfg.database}"

    def _get_timeout(self) -> str | None:
        """返回 Oracle 的会话超时 SQL。

        Args:
            无。

        Returns:
            Oracle 当前不注入通用 SET 语句，因此返回 None。
        """
        return None

    async def execute(self, sql: str, params: dict | None = None):
        """在线程池中执行 SQL 并转换为统一行字典。

        Args:
            sql: 只读 SQL 语句。
            params: 命名参数映射。

        Returns:
            查询结果的字典列表。
        """
        logger.debug("Oracle 执行入口", datasource=self.config.name, sql_preview=sql[:120])

        if self._engine is None:
            await self.create_engine()

        def _run():
            with self._engine.connect() as conn:
                rows = conn.execute(sa.text(sql), params or {}).fetchall()
                return [dict(row._mapping) for row in rows]

        try:
            result = await asyncio.to_thread(_run)
        except Exception as exc:
            logger.error(
                "Oracle 执行失败",
                datasource=self.config.name,
                error=str(exc)[:500],
                exc_info=True,
            )
            raise
        logger.info("Oracle 执行完成", datasource=self.config.name, row_count=len(result))
        return result

    async def explain(self, sql: str) -> dict:
        """执行 Oracle EXPLAIN PLAN 语义校验。

        Args:
            sql: 待校验的 SQL 语句。

        Returns:
            包含 valid 和 errors 的校验结果。
        """
        logger.debug("Oracle explain 入口", datasource=self.config.name, sql_preview=sql[:120])
        try:
            await self.execute(f"EXPLAIN PLAN FOR {sql}")
            logger.info("Oracle explain 完成", datasource=self.config.name, valid=True)
            return {"valid": True, "errors": []}
        except Exception as e:
            logger.error(
                "Oracle explain 失败",
                datasource=self.config.name,
                error=str(e)[:500],
                exc_info=True,
            )
            return {"valid": False, "errors": [{"type": "semantic_error", "message": str(e)[:500]}]}

    async def health_check(self) -> bool:
        """使用 Oracle 专属 DUAL 表检查连接。

        Args:
            无。

        Returns:
            连接可用返回 True，否则返回 False。
        """
        logger.debug("Oracle 健康检查入口", datasource=self.config.name)
        try:
            await self.execute("SELECT 1 FROM DUAL")
            logger.info("Oracle 健康检查完成", datasource=self.config.name, healthy=True)
            return True
        except Exception as exc:
            logger.error(
                "Oracle 健康检查失败",
                datasource=self.config.name,
                error=str(exc)[:500],
                exc_info=True,
            )
            return False

    async def create_engine(self) -> Any:
        """创建同步 Oracle Engine，供线程池执行异步适配。

        Args:
            无，使用构造时保存的数据源配置。

        Returns:
            已缓存的同步 SQLAlchemy Engine。
        """
        logger.debug("Oracle 引擎创建入口", datasource=self.config.name)
        try:
            self._engine = sa.create_engine(
                self._build_url(),
                pool_size=2,
                max_overflow=5,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
        except Exception as exc:
            logger.error(
                "Oracle 引擎创建失败",
                datasource=self.config.name,
                error=str(exc)[:500],
                exc_info=True,
            )
            raise
        logger.info("Oracle 引擎创建完成", datasource=self.config.name)
        return self._engine

    async def close(self) -> None:
        """在线程池中释放同步 Oracle Engine。

        Args:
            无。

        Returns:
            无返回值。
        """
        logger.debug("Oracle 引擎关闭入口", datasource=self.config.name)
        if self._engine is not None:
            try:
                await asyncio.to_thread(self._engine.dispose)
            except Exception as exc:
                logger.error(
                    "Oracle 引擎关闭失败",
                    datasource=self.config.name,
                    error=str(exc)[:500],
                    exc_info=True,
                )
                raise
            finally:
                self._engine = None
        logger.info("Oracle 引擎关闭完成", datasource=self.config.name)
