"""ClickHouse 连接器 — 3.2.1~5。"""

from __future__ import annotations

from typing import Any

from src.connectors.base import ConnectorBase
from src.logging_config import get_logger

logger = get_logger(__name__)


class ClickHouseConnector(ConnectorBase):
    """基于 clickhouse-connect 的 ClickHouse 客户端连接器。"""

    def _build_url(self) -> str:
        """构建兼容旧配置展示的 ClickHouse URL。

        Args:
            无，使用当前数据源配置。

        Returns:
            旧版 SQLAlchemy URL 文本；实际连接由 clickhouse-connect 创建。
        """
        c = self.config
        return (
            f"clickhouse+asynch://{c.username}:{c.password}"
            f"@{c.host}:{c.port}/{c.database}"
        )

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
        import clickhouse_connect

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
        self._engine = clickhouse_connect.get_client(
            host=self.config.host,
            port=http_port,
            username=self.config.username,
            password=self.config.password,
            database=self.config.database or "default",
        )
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
            result = self._engine.query(bound_sql, parameters=bound_params)
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

    async def close(self) -> None:
        """关闭 ClickHouse 客户端连接。

        Args:
            无。

        Returns:
            无返回值。
        """
        logger.debug("ClickHouse 客户端关闭入口", datasource=self.config.name)
        if self._engine is not None:
            self._engine.close()
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
        except Exception:
            return ""
