"""Oracle 连接器 -- oracledb 驱动，通过线程池适配异步。"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy import URL

from src.connectors.base import ConnectorBase
from src.connectors.registry import register_connector
from src.logging_config import get_logger

logger = get_logger(__name__)

_ORACLE_SCHEMA_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}\Z")


@register_connector("oracle")
class OracleConnector(ConnectorBase):
    """使用同步 SQLAlchemy Engine 并在线程池中执行 Oracle 操作。"""

    explain_template = "EXPLAIN PLAN FOR {sql}"
    probe_sql = "SELECT 1 FROM DUAL"

    def _build_url(self) -> URL:
        """构建 Oracle service name 连接 URL。

        Args:
            无，使用构造时保存的数据源配置。

        Returns:
            包含 service_name 参数的 SQLAlchemy URL。
        """
        cfg = self.config
        logger.debug("Oracle URL 构建入口", datasource=cfg.name)
        result = URL.create(
            "oracle+oracledb",
            username=cfg.username or None,
            password=cfg.password or None,
            host=cfg.host or None,
            port=cfg.port or None,
            query={"service_name": cfg.database},
        )
        logger.info("Oracle URL 构建完成", datasource=cfg.name)
        return result

    def _get_timeout(self) -> str | None:
        """返回 Oracle 的会话超时 SQL。

        Args:
            无。

        Returns:
            Oracle 当前不注入通用 SET 语句，因此返回 None。
        """
        return None

    # 方法作用：读取并校验 Oracle 会话要切换到的业务 schema。
    # Args: 无，使用数据源 extra_params.schema 配置。
    # Returns: 合法 schema 名称；未配置时返回空字符串。
    def _get_current_schema(self) -> str:
        """返回可安全写入 ALTER SESSION 的 Oracle schema 标识符。"""
        schema = str(self.config.extra_params.get("schema", "") or "").strip()
        if schema and not _ORACLE_SCHEMA_PATTERN.fullmatch(schema):
            logger.error(
                "Oracle schema 配置非法",
                datasource=self.config.name,
                schema=schema[:128],
            )
            raise ValueError("Oracle schema 必须是合法的未引用标识符")
        return schema

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
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

        # 方法作用：在同步 Oracle 连接中执行语句并按结果集类型提取行。
        # Args: 无，使用外层 sql 和 params 参数。
        # Returns: 查询行字典列表，无结果集语句返回空列表。
        def _run() -> list[dict[str, Any]]:
            with self._engine.connect() as conn:
                result = conn.execute(sa.text(sql), params or {})
                returns_rows = bool(
                    getattr(result, "returns_rows", hasattr(result, "fetchall"))
                )
                logger.info(
                    "Oracle 执行结果边界",
                    datasource=self.config.name,
                    returns_rows=returns_rows,
                )
                rows = result.fetchall() if returns_rows else []
                return self.rows_to_dict_list(rows)

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
            current_schema = self._get_current_schema()
            self._engine = sa.create_engine(
                self._build_url(),
                pool_size=2,
                max_overflow=5,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
            if current_schema:
                # 方法作用：在每个新 Oracle DBAPI 连接上切换业务 schema。
                # Args: dbapi_connection - 原生连接；connection_record - SQLAlchemy 连接记录。
                # Returns: 无返回值。
                def _set_current_schema(dbapi_connection, connection_record) -> None:
                    cursor = dbapi_connection.cursor()
                    try:
                        cursor.execute(
                            f'ALTER SESSION SET CURRENT_SCHEMA = "{current_schema}"'
                        )
                    finally:
                        cursor.close()

                sa.event.listen(self._engine, "connect", _set_current_schema)
                logger.info(
                    "Oracle 会话 schema 配置完成",
                    datasource=self.config.name,
                    schema=current_schema,
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
