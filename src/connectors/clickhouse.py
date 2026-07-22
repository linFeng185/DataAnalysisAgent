"""ClickHouse 连接器 — 3.2.1~5。"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from sqlalchemy import URL

from src.connectors.base import ConnectorBase
from src.connectors.registry import register_connector
from src.config import get_settings
from src.logging_config import get_logger
from src.security.network import validate_outbound_host

logger = get_logger(__name__)


class ClickHouseRow(dict):
    """为 ClickHouse 字典结果提供 SQLAlchemy Row 的 `_mapping` 属性。"""

    @property
    def _mapping(self) -> "ClickHouseRow":
        """返回自身作为 RowMapping。

        Args:
            无。

        Returns:
            当前字典行。
        """
        logger.debug("ClickHouseRow._mapping 入口")
        logger.info("ClickHouseRow._mapping 完成")
        return self


class ClickHouseResult:
    """将 clickhouse-connect 查询结果适配为 SQLAlchemy 风格结果。"""

    # 方法作用：把列名和元组行转换为可迭代字典行。
    # Args: column_names - 列名；rows - 原始结果元组。
    # Returns: 无返回值。
    def __init__(self, column_names: list[str], rows: list[tuple]) -> None:
        logger.debug("ClickHouseResult.__init__ 入口", row_count=len(rows))
        self._rows = [ClickHouseRow(dict(zip(column_names, row))) for row in rows]
        self._cursor = 0
        logger.info("ClickHouseResult.__init__ 完成", row_count=len(rows))

    # 方法作用：返回游标后的全部结果并消费游标。
    # Args: 无。
    # Returns: 剩余 ClickHouseRow 列表。
    def fetchall(self) -> list[ClickHouseRow]:
        """返回剩余全部行。"""
        logger.debug("ClickHouseResult.fetchall 入口", cursor=self._cursor)
        rows = self._rows[self._cursor:]
        self._cursor = len(self._rows)
        logger.info("ClickHouseResult.fetchall 完成", row_count=len(rows))
        return rows

    # 方法作用：按迭代协议消费剩余结果。
    # Args: 无。
    # Returns: 剩余 ClickHouseRow 的迭代器。
    def __iter__(self):
        """与 fetchall 共享游标。"""
        logger.debug("ClickHouseResult.__iter__ 入口", cursor=self._cursor)
        result = iter(self.fetchall())
        logger.info("ClickHouseResult.__iter__ 完成")
        return result

    # 方法作用：从当前游标返回最多 size 行。
    # Args: size - 最大行数。
    # Returns: 结果行列表。
    def fetchmany(self, size: int) -> list[ClickHouseRow]:
        """有界读取 ClickHouse 查询结果。"""
        logger.debug("ClickHouseResult.fetchmany 入口", cursor=self._cursor, size=size)
        rows = self._rows[self._cursor:self._cursor + size]
        self._cursor += len(rows)
        logger.info("ClickHouseResult.fetchmany 完成", row_count=len(rows))
        return rows

    # 方法作用：释放结果集内存和游标状态。
    # Args: 无。
    # Returns: 无返回值。
    def close(self) -> None:
        """清空结果缓存。"""
        logger.debug("ClickHouseResult.close 入口", row_count=len(self._rows))
        self._rows = []
        self._cursor = 0
        logger.info("ClickHouseResult.close 完成")

    # 方法作用：允许异步 Provider 对同步结果使用 await。
    # Args: 无。
    # Returns: 解析为自身的 awaitable 迭代器。
    def __await__(self):
        """兼容 `await connection.execute()` 消费方式。"""
        logger.debug("ClickHouseResult.__await__ 入口")

        # 方法作用：异步返回当前结果对象。
        # Args: 无。
        # Returns: 当前 ClickHouseResult。
        async def resolved() -> "ClickHouseResult":
            logger.debug("ClickHouseResult.resolved 入口")
            logger.info("ClickHouseResult.resolved 完成")
            return self

        logger.info("ClickHouseResult.__await__ 完成")
        return resolved().__await__()


class ClickHouseConnection:
    """适配 clickhouse-connect Client 的同步和异步连接上下文。"""

    # 方法作用：保存底层 clickhouse-connect 客户端。
    # Args: client - ClickHouse 客户端。
    # Returns: 无返回值。
    def __init__(self, client: Any) -> None:
        logger.debug("ClickHouseConnection.__init__ 入口")
        self._client = client
        logger.info("ClickHouseConnection.__init__ 完成")

    # 方法作用：执行 SQL 并返回 SQLAlchemy 风格结果。
    # Args: statement - SQLAlchemy TextClause 或 SQL 文本；params - 命名参数。
    # Returns: ClickHouseResult。
    def execute(self, statement: Any, params: dict | None = None) -> ClickHouseResult:
        """执行同步 clickhouse-connect 查询。"""
        sql, bound = ClickHouseConnector._bind_parameters(str(statement), params)
        logger.debug("ClickHouseConnection.execute 入口", sql=sql)
        result = self._client.query(sql, parameters=bound)
        adapted = ClickHouseResult(result.column_names, result.result_rows)
        logger.info("ClickHouseConnection.execute 完成", row_count=len(result.result_rows))
        return adapted

    # 方法作用：兼容 SQLAlchemy execution_options 链式调用。
    # Args: kwargs - 执行选项。
    # Returns: 当前连接对象。
    def execution_options(self, **kwargs: Any) -> "ClickHouseConnection":
        """ClickHouse 客户端无需额外执行选项。"""
        logger.debug("ClickHouseConnection.execution_options 入口", options=sorted(kwargs))
        logger.info("ClickHouseConnection.execution_options 完成")
        return self

    # 方法作用：进入同步连接上下文。
    # Args: 无。
    # Returns: 当前连接对象。
    def __enter__(self) -> "ClickHouseConnection":
        logger.debug("ClickHouseConnection.__enter__ 入口")
        logger.info("ClickHouseConnection.__enter__ 完成")
        return self

    # 方法作用：退出同步连接上下文。
    # Args: exc_type - 异常类型；exc - 异常；tb - 堆栈。
    # Returns: False，不吞掉异常。
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        logger.debug("ClickHouseConnection.__exit__ 入口", has_error=exc is not None)
        logger.info("ClickHouseConnection.__exit__ 完成")
        return False

    # 方法作用：进入异步连接上下文。
    # Args: 无。
    # Returns: 当前连接对象。
    async def __aenter__(self) -> "ClickHouseConnection":
        logger.debug("ClickHouseConnection.__aenter__ 入口")
        logger.info("ClickHouseConnection.__aenter__ 完成")
        return self

    # 方法作用：退出异步连接上下文。
    # Args: exc_type - 异常类型；exc - 异常；tb - 堆栈。
    # Returns: False，不吞掉异常。
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        logger.debug("ClickHouseConnection.__aexit__ 入口", has_error=exc is not None)
        logger.info("ClickHouseConnection.__aexit__ 完成")
        return False


class ClickHouseEngine:
    """为 Registry 提供统一 connect/dispose 边界的 ClickHouse 引擎适配器。"""

    # 方法作用：保存 ClickHouse 客户端。
    # Args: client - clickhouse-connect 客户端。
    # Returns: 无返回值。
    def __init__(self, client: Any) -> None:
        logger.debug("ClickHouseEngine.__init__ 入口")
        self.client = client
        logger.info("ClickHouseEngine.__init__ 完成")

    # 方法作用：创建 ClickHouse 连接上下文。
    # Args: 无。
    # Returns: ClickHouseConnection。
    def connect(self) -> ClickHouseConnection:
        logger.debug("ClickHouseEngine.connect 入口")
        connection = ClickHouseConnection(self.client)
        logger.info("ClickHouseEngine.connect 完成")
        return connection

    # 方法作用：关闭底层 ClickHouse 客户端。
    # Args: 无。
    # Returns: 无返回值。
    def dispose(self) -> None:
        logger.debug("ClickHouseEngine.dispose 入口")
        self.client.close()
        logger.info("ClickHouseEngine.dispose 完成")


@register_connector("clickhouse")
class ClickHouseConnector(ConnectorBase):
    """基于 clickhouse-connect 的 ClickHouse 客户端连接器。"""

    explain_template = "EXPLAIN SYNTAX {sql}"

    @property
    def timeout_sql(self) -> str:
        """返回 ClickHouse 查询超时设置。

        Args:
            无。

        Returns:
            SET max_execution_time SQL。
        """
        logger.debug("ClickHouse 超时 SQL 入口", datasource=self.config.name)
        result = f"SET max_execution_time = {get_settings().max_execution_time}"
        logger.info("ClickHouse 超时 SQL 完成", datasource=self.config.name)
        return result

    def _build_url(self) -> URL:
        """构建默认隐藏密码的 ClickHouse SQLAlchemy URL。

        Args:
            无，使用当前数据源配置。

        Returns:
            SQLAlchemy URL 对象；实际连接由 clickhouse-connect 创建。
        """
        c = self.config
        logger.debug("ClickHouse URL 构建入口", datasource=c.name)
        result = URL.create(
            "clickhouse+asynch",
            username=c.username or None,
            password=c.password or None,
            host=c.host or None,
            port=c.port or None,
            database=c.database or None,
        )
        logger.info("ClickHouse URL 构建完成", datasource=c.name)
        return result

    @staticmethod
    def _bind_parameters(sql: str, params: dict | None) -> tuple[str, dict | None]:
        """将 :name 参数转换为 ClickHouse 参数语法。

        Args:
            sql: 原始 SQL。
            params: 命名参数映射。

        Returns:
            可由 clickhouse-connect 执行的 SQL 和参数映射。
        """
        if not params:
            return sql, None
        for name, value in params.items():
            type_name = "String"
            if isinstance(value, bool):
                type_name = "Bool"
            elif isinstance(value, int):
                type_name = "Int64"
            elif isinstance(value, float):
                type_name = "Float64"
            sql = sql.replace(f":{name}", f"{{{name}:{type_name}}}")
        return sql, params

    async def create_engine(self) -> Any:
        """创建并缓存 ClickHouse 客户端。

        Args:
            无，使用当前数据源配置。

        Returns:
            clickhouse-connect Client 实例。
        """
        configured_port = self.config.port
        http_port = int(
            self.config.extra_params.get("http_port")
            or (8123 if configured_port in (0, 9000) else configured_port)
        )
        logger.debug(
            "ClickHouse 客户端创建入口",
            datasource=self.config.name,
            host=self.config.host,
            configured_port=configured_port,
            http_port=http_port,
        )
        connect_timeout = max(1, int(self.config.extra_params.get("connect_timeout", 5)))
        query_retries = max(0, int(self.config.extra_params.get("query_retries", 0)))

        # 方法作用：在线程池中完成 TCP 探针和同步客户端创建。
        # Args: 无，使用闭包中的连接配置。
        # Returns: clickhouse-connect 客户端。
        def create_client():
            logger.debug(
                "ClickHouse TCP 探针入口",
                datasource=self.config.name,
                host=self.config.host,
                port=http_port,
                timeout=connect_timeout,
            )
            settings = get_settings()
            validated_addresses = validate_outbound_host(
                self.config.host,
                http_port,
                getattr(settings, "datasource_host_allowlist", ""),
            )
            validated_host = validated_addresses[0]
            with socket.create_connection(
                (validated_host, http_port),
                timeout=connect_timeout,
            ):
                logger.info(
                    "ClickHouse TCP 探针完成",
                    datasource=self.config.name,
                    validated_host=validated_host,
                )
            import clickhouse_connect

            client = clickhouse_connect.get_client(
                host=validated_host,
                port=http_port,
                username=self.config.username,
                password=self.config.password,
                database=self.config.database or "default",
                connect_timeout=connect_timeout,
                query_retries=query_retries,
            )
            logger.info("ClickHouse 同步客户端创建完成", datasource=self.config.name)
            return client

        client = await asyncio.to_thread(create_client)
        self._engine = ClickHouseEngine(client)
        logger.info("ClickHouse 客户端创建完成", datasource=self.config.name)
        return self._engine

    async def execute(self, sql: str, params: dict | None = None) -> list[dict]:
        """执行 ClickHouse 查询并返回字典列表。

        Args:
            sql: SQL 语句。
            params: 命名参数映射。

        Returns:
            查询结果字典列表。
        """
        if self._engine is None:
            await self.create_engine()
        bound_sql, bound_params = self._bind_parameters(sql, params)
        logger.debug("ClickHouse 执行入口", datasource=self.config.name, sql_preview=sql[:120])
        try:
            result = await asyncio.to_thread(
                self._engine.client.query,
                bound_sql,
                parameters=bound_params,
            )
            rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
        except Exception as exc:
            logger.error(
                "ClickHouse 执行失败",
                datasource=self.config.name,
                error=str(exc)[:500],
                exc_info=True,
            )
            raise
        logger.info("ClickHouse 执行完成", datasource=self.config.name, row_count=len(rows))
        return rows

    # 方法作用：有界执行 ClickHouse 查询并报告截断状态。
    # Args: sql - SQL 语句；max_rows - 最大返回行数；params - 命名参数。
    # Returns: 字典行列表和是否截断。
    async def execute_bounded(
        self,
        sql: str,
        max_rows: int,
        params: dict | None = None,
    ) -> tuple[list[dict], bool]:
        """在线程池执行同步客户端，并在返回边界截断结果。"""
        logger.debug(
            "ClickHouse 有界执行入口",
            datasource=self.config.name,
            max_rows=max_rows,
            sql=sql,
        )
        await self.execute(self.timeout_sql)
        rows = await self.execute(sql, params)
        truncated = len(rows) > max_rows
        bounded = rows[:max_rows]
        logger.info(
            "ClickHouse 有界执行完成",
            datasource=self.config.name,
            row_count=len(bounded),
            truncated=truncated,
        )
        return bounded, truncated

    async def close(self) -> None:
        """关闭 ClickHouse 客户端连接。

        Args:
            无。

        Returns:
            无返回值。
        """
        logger.debug("ClickHouse 客户端关闭入口", datasource=self.config.name)
        if self._engine is not None:
            await asyncio.to_thread(self._engine.dispose)
            self._engine = None
        logger.info("ClickHouse 客户端关闭完成", datasource=self.config.name)

    async def get_partition_key(self, table: str) -> str:
        """3.2.5 获取 ClickHouse 表分区键。"""
        try:
            rows = await self.execute(
                "SELECT partition_key FROM system.tables "
                "WHERE database = :db AND name = :table",
                {"db": self.config.database, "table": table},
            )
            return rows[0].get("partition_key", "") if rows else ""
        except Exception as exc:
            logger.warning(
                "ClickHouse 分区键读取失败",
                datasource=self.config.name,
                table=table,
                error=str(exc),
                exc_info=True,
            )
            return ""


_ClickHouseResult = ClickHouseResult
_ClickHouseRow = ClickHouseRow
_ClickHouseConnection = ClickHouseConnection
_ClickHouseEngine = ClickHouseEngine
