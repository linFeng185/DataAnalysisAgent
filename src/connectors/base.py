"""连接器基类，统一引擎、执行、超时、EXPLAIN 和健康检查边界。"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.config import get_settings
from src.datasource.config import DataSourceConfig
from src.logging_config import get_logger


logger = get_logger(__name__)


class ConnectorBase(ABC):
    """数据库连接器抽象基类。"""

    explain_template: str | None = None
    probe_sql: str = "SELECT 1"

    # 方法作用：保存数据源配置并初始化空引擎引用。
    # Args: config - 已归一化的数据源配置。
    # Returns: 无返回值。
    def __init__(self, config: DataSourceConfig) -> None:
        logger.debug("ConnectorBase.__init__ 入口", datasource=config.name, dialect=config.dialect)
        self.config = config
        self._engine: Any | None = None
        logger.info("ConnectorBase.__init__ 完成", datasource=config.name, dialect=config.dialect)

    # 方法作用：创建并缓存通用 SQLAlchemy AsyncEngine。
    # Args: 无，使用构造时保存的数据源配置。
    # Returns: 创建完成的 AsyncEngine。
    async def create_engine(self) -> AsyncEngine:
        """使用方言 URL 和连接器参数创建异步连接池。"""
        settings = get_settings()
        logger.debug("连接器引擎创建入口", datasource=self.config.name, dialect=self.config.dialect)
        try:
            self._engine = create_async_engine(
                self._build_url(),
                **self.engine_kwargs,
                echo=settings.env == "dev",
            )
        except Exception as exc:
            logger.error(
                "连接器引擎创建失败",
                datasource=self.config.name,
                dialect=self.config.dialect,
                error=str(exc),
                exc_info=True,
            )
            raise
        logger.info("连接器引擎创建完成", datasource=self.config.name, dialect=self.config.dialect)
        return self._engine

    @property
    def engine_kwargs(self) -> dict[str, Any]:
        """返回通用 SQLAlchemy 引擎参数。

        Args:
            无。

        Returns:
            可传给 create_async_engine 的参数字典。
        """
        logger.debug("连接器引擎参数入口", datasource=self.config.name)
        result = {
            "pool_size": self.config.extra_params.get("pool_size", 5),
            "max_overflow": self.config.extra_params.get("max_overflow", 10),
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        }
        logger.info("连接器引擎参数完成", datasource=self.config.name)
        return result

    @property
    def timeout_sql(self) -> str | None:
        """返回当前方言会话超时 SQL。

        Args:
            无。

        Returns:
            默认不注入超时 SQL。
        """
        logger.debug("连接器超时 SQL 入口", datasource=self.config.name)
        logger.info("连接器超时 SQL 完成", datasource=self.config.name, configured=False)
        return None

    @abstractmethod
    def _build_url(self) -> str:
        """构建方言特定连接 URL。

        Args:
            无。

        Returns:
            SQLAlchemy 或兼容驱动 URL。
        """
        raise NotImplementedError

    @property
    def engine(self) -> Any | None:
        """返回当前缓存引擎。

        Args:
            无。

        Returns:
            引擎或 None。
        """
        logger.debug("连接器引擎读取入口", datasource=self.config.name)
        logger.info("连接器引擎读取完成", datasource=self.config.name, available=self._engine is not None)
        return self._engine

    # 方法作用：复用 Registry 已创建的引擎，避免 Tool 或节点重复建池。
    # Args: engine - SQLAlchemy 引擎或方言适配引擎。
    # Returns: 当前连接器实例。
    def attach_engine(self, engine: Any) -> "ConnectorBase":
        """把外部已有引擎绑定到连接器。"""
        logger.debug("连接器绑定引擎入口", datasource=self.config.name)
        self._engine = engine
        logger.info("连接器绑定引擎完成", datasource=self.config.name)
        return self

    # 方法作用：执行 SQL 并返回全部结果字典。
    # Args: sql - SQL 语句；params - 命名参数。
    # Returns: 字典结果行列表。
    async def execute(self, sql: str, params: dict | None = None) -> list[dict]:
        """兼容异步和同步 SQLAlchemy 引擎。"""
        logger.debug("连接器执行入口", datasource=self.config.name, sql=sql)
        if not self._engine:
            await self.create_engine()
        try:
            if isinstance(self._engine, AsyncEngine):
                async with self._engine.connect() as connection:
                    if self.timeout_sql:
                        await connection.execute(sa.text(self.timeout_sql))
                    result = await connection.execute(sa.text(sql), params or {})
                    rows = self.rows_to_dict_list(result)
            else:
                rows = await asyncio.to_thread(self._execute_sync, sql, params)
        except Exception as exc:
            logger.error(
                "连接器执行失败",
                datasource=self.config.name,
                error=str(exc),
                exc_info=True,
            )
            raise
        logger.info("连接器执行完成", datasource=self.config.name, row_count=len(rows))
        return rows

    # 方法作用：在线程池调用方内执行同步 SQLAlchemy 查询。
    # Args: sql - SQL 语句；params - 命名参数。
    # Returns: 字典结果行列表。
    def _execute_sync(self, sql: str, params: dict | None = None) -> list[dict]:
        """封装同步连接生命周期。"""
        logger.debug("同步连接器执行入口", datasource=self.config.name, sql=sql)
        with self._engine.connect() as connection:
            if self.timeout_sql:
                connection.execute(sa.text(self.timeout_sql))
            if params:
                result = connection.execute(sa.text(sql), params)
            else:
                result = connection.execute(sa.text(sql))
            rows = self.rows_to_dict_list(result)
        logger.info("同步连接器执行完成", datasource=self.config.name, row_count=len(rows))
        return rows

    # 方法作用：有界读取查询结果，避免大结果集占满内存。
    # Args: sql - SQL 语句；max_rows - 最大返回行数；params - 命名参数。
    # Returns: 结果行列表与是否截断。
    async def execute_bounded(
        self,
        sql: str,
        max_rows: int,
        params: dict | None = None,
    ) -> tuple[list[dict], bool]:
        """对异步和同步 SQLAlchemy 引擎提供统一有界读取。"""
        logger.debug("连接器有界执行入口", datasource=self.config.name, max_rows=max_rows, sql=sql)
        if not self._engine:
            await self.create_engine()
        try:
            if isinstance(self._engine, AsyncEngine):
                async with self._engine.connect() as connection:
                    if self.timeout_sql:
                        await connection.execute(sa.text(self.timeout_sql))
                    if params:
                        result = await connection.stream(sa.text(sql), params)
                    else:
                        result = await connection.stream(sa.text(sql))
                    rows: list[dict] = []
                    async for row in result:
                        rows.append(self._row_to_dict(row))
                        if len(rows) > max_rows:
                            break
                    await result.close()
            else:
                rows = await asyncio.to_thread(self._execute_bounded_sync, sql, max_rows, params)
        except Exception as exc:
            logger.error(
                "连接器有界执行失败",
                datasource=self.config.name,
                error=str(exc),
                exc_info=True,
            )
            raise
        truncated = len(rows) > max_rows
        bounded = rows[:max_rows]
        logger.info(
            "连接器有界执行完成",
            datasource=self.config.name,
            row_count=len(bounded),
            truncated=truncated,
        )
        return bounded, truncated

    # 方法作用：同步引擎分批读取最多 max_rows + 1 行。
    # Args: sql - SQL 语句；max_rows - 最大返回行数；params - 命名参数。
    # Returns: 用于判断截断的字典行列表。
    def _execute_bounded_sync(
        self,
        sql: str,
        max_rows: int,
        params: dict | None = None,
    ) -> list[dict]:
        """封装同步引擎的流式结果生命周期。"""
        logger.debug("同步连接器有界执行入口", datasource=self.config.name, max_rows=max_rows)
        with self._engine.connect() as connection:
            if self.timeout_sql:
                connection.execute(sa.text(self.timeout_sql))
            execution_connection = connection.execution_options(stream_results=True)
            if params:
                result = execution_connection.execute(sa.text(sql), params)
            else:
                result = execution_connection.execute(sa.text(sql))
            fetched = result.fetchmany(max_rows + 1)
            result.close()
        rows = [self._row_to_dict(row) for row in fetched]
        logger.info("同步连接器有界执行完成", datasource=self.config.name, row_count=len(rows))
        return rows

    # 方法作用：用方言模板执行 EXPLAIN 语义校验。
    # Args: sql - 待校验 SQL。
    # Returns: valid 和 errors 字段。
    async def explain(self, sql: str) -> dict:
        """未配置模板或显式跳过的方言直接视为有效。"""
        settings = get_settings()
        logger.debug("连接器 EXPLAIN 入口", datasource=self.config.name, sql=sql)
        if self.config.dialect in settings.explain_skip_dialects:
            logger.warning("连接器 EXPLAIN 跳过", datasource=self.config.name, reason="配置跳过")
            return {"valid": True, "errors": []}
        if not self.explain_template:
            logger.warning("连接器 EXPLAIN 跳过", datasource=self.config.name, reason="模板为空")
            return {"valid": True, "errors": []}
        try:
            await self.execute(self.explain_template.format(sql=sql))
        except Exception as exc:
            logger.error("连接器 EXPLAIN 失败", datasource=self.config.name, error=str(exc), exc_info=True)
            return {
                "valid": False,
                "errors": [{
                    "type": "semantic_error",
                    "message": str(exc).split("Stack trace:")[0][:500],
                }],
            }
        logger.info("连接器 EXPLAIN 完成", datasource=self.config.name, valid=True)
        return {"valid": True, "errors": []}

    # 方法作用：执行方言健康探针。
    # Args: 无。
    # Returns: 连接可用返回 True。
    async def health_check(self) -> bool:
        """健康检查失败只返回 False，同时保留完整日志。"""
        logger.debug("连接器健康检查入口", datasource=self.config.name, probe_sql=self.probe_sql)
        try:
            await self.execute(self.probe_sql)
        except Exception as exc:
            logger.error("连接器健康检查失败", datasource=self.config.name, error=str(exc), exc_info=True)
            return False
        logger.info("连接器健康检查完成", datasource=self.config.name, healthy=True)
        return True

    # 方法作用：关闭同步或异步引擎并清空引用。
    # Args: 无。
    # Returns: 无返回值。
    async def close(self) -> None:
        """幂等释放连接池。"""
        logger.debug("连接器关闭入口", datasource=self.config.name)
        if self._engine:
            try:
                dispose_result = self._engine.dispose()
                if inspect.isawaitable(dispose_result):
                    await dispose_result
            except Exception as exc:
                logger.error("连接器关闭失败", datasource=self.config.name, error=str(exc), exc_info=True)
                raise
            finally:
                self._engine = None
        logger.info("连接器关闭完成", datasource=self.config.name)

    # 方法作用：将结果集合转换为字典列表。
    # Args: rows - SQLAlchemy 结果集合或可迭代行。
    # Returns: 字典结果列表。
    @staticmethod
    def rows_to_dict_list(rows: Any) -> list[dict]:
        """统一 RowMapping 和精度转换。"""
        logger.debug("连接器结果格式化入口")
        result = [ConnectorBase._row_to_dict(row) for row in rows]
        logger.info("连接器结果格式化完成", row_count=len(result))
        return result

    # 方法作用：将单行结果转换为字典并把 float 转为 Decimal。
    # Args: row - SQLAlchemy Row 或兼容行。
    # Returns: 可安全用于精确计算的字典行。
    @staticmethod
    def _row_to_dict(row: Any) -> dict:
        """保持执行节点原有的数值精度契约。"""
        logger.debug("连接器单行格式化入口", row_type=type(row).__name__)
        result = {
            key: Decimal(str(value))
            if isinstance(value, float) and not isinstance(value, bool)
            else value
            for key, value in dict(row._mapping).items()
        }
        logger.info("连接器单行格式化完成", column_count=len(result))
        return result

    # 方法作用：兼容旧调用路径读取 timeout_sql。
    # Args: 无。
    # Returns: 方言超时 SQL 或 None。
    def _get_timeout(self) -> str | None:
        """旧测试和插件可继续调用该方法。"""
        logger.debug("连接器兼容超时入口", datasource=self.config.name)
        result = self.timeout_sql
        logger.info("连接器兼容超时完成", datasource=self.config.name, configured=bool(result))
        return result


# 方法作用：兼容旧导入路径并委托统一连接器注册表。
# Args: ds - 数据源配置。
# Returns: 对应方言的 ConnectorBase 实例。
def create_connector(ds: DataSourceConfig) -> ConnectorBase:
    """兼容 `src.connectors.base.create_connector`。"""
    logger.debug("兼容连接器工厂入口", datasource=ds.name, dialect=ds.dialect)
    from src.connectors.registry import create_connector as create_registered_connector

    connector = create_registered_connector(ds)
    logger.info("兼容连接器工厂完成", datasource=ds.name, dialect=ds.dialect)
    return connector
