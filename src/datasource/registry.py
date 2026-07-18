"""DataSourceRegistry — 数据源统一注册与解析入口。"""

from __future__ import annotations

import asyncio
import inspect

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.config import get_settings
from src.datasource.config import DataSourceConfig
from src.datasource.credential_manager import CredentialManager
from src.datasource.providers.base import DataSourceProvider
from src.exceptions import DataSourceNotFoundError
from src.logging_config import get_logger

logger = get_logger(__name__)


class _ClickHouseResult:
    """将 clickhouse-connect 查询结果适配为 SQLAlchemy 风格结果。"""

    def __init__(self, column_names: list[str], rows: list[tuple]) -> None:
        self._rows = [_ClickHouseRow(dict(zip(column_names, row))) for row in rows]
        self._cursor = 0

    def fetchall(self) -> list["_ClickHouseRow"]:
        """返回剩余全部行。"""
        rows = self._rows[self._cursor:]
        self._cursor = len(self._rows)
        return rows

    def __iter__(self):
        """支持 for row in result 迭代（与 fetchall 共享游标）。

        Returns:
            剩余行的迭代器，消费后游标移动到末尾。
        """
        return iter(self.fetchall())

    def fetchmany(self, size: int) -> list["_ClickHouseRow"]:
        """返回最多 size 行，支持结果集有界读取。"""
        rows = self._rows[self._cursor:self._cursor + size]
        self._cursor += len(rows)
        return rows

    def close(self) -> None:
        """释放适配结果的游标状态。"""
        self._rows = []
        self._cursor = 0

    def __await__(self):
        """允许异步 Provider 对同步客户端结果使用 await。"""
        async def _resolved():
            return self

        return _resolved().__await__()


class _ClickHouseRow(dict):
    """为字典结果提供 SQLAlchemy Row 的 _mapping 属性。"""

    @property
    def _mapping(self) -> "_ClickHouseRow":
        """返回自身作为 RowMapping。"""
        return self


class _ClickHouseConnection:
    """适配 clickhouse-connect Client 的同步/异步上下文连接。"""

    def __init__(self, client) -> None:
        self._client = client

    @staticmethod
    def _bind_parameters(sql: str, params: dict | None) -> tuple[str, dict | None]:
        """将 SQLAlchemy :name 占位符转换为 ClickHouse 命名参数。

        Args:
            sql: 原始 SQL 文本。
            params: 命名参数映射。

        Returns:
            ClickHouse 参数语法 SQL 和参数映射。
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

    def execute(self, statement, params: dict | None = None) -> _ClickHouseResult:
        """执行 ClickHouse 查询并返回可分页结果。"""
        sql, bound = self._bind_parameters(str(statement), params)
        result = self._client.query(sql, parameters=bound)
        return _ClickHouseResult(result.column_names, result.result_rows)

    def execution_options(self, **kwargs) -> "_ClickHouseConnection":
        """兼容 SQLAlchemy execution_options 链式调用。"""
        return self

    def __enter__(self) -> "_ClickHouseConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    async def __aenter__(self) -> "_ClickHouseConnection":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _ClickHouseEngine:
    """提供 SQLAlchemy Engine 所需 connect/dispose 边界。"""

    def __init__(self, client) -> None:
        self._client = client

    def connect(self) -> _ClickHouseConnection:
        """创建客户端连接上下文。"""
        return _ClickHouseConnection(self._client)

    def dispose(self) -> None:
        """关闭 ClickHouse 客户端。"""
        self._client.close()


# 模块级单例 — 启动时填充，请求时复用
_registry: DataSourceRegistry | None = None


def get_registry() -> "DataSourceRegistry":
    """获取进程级数据源 Registry 单例。

    Returns:
        全局 DataSourceRegistry 实例。
    """
    global _registry
    if _registry is None:
        _registry = DataSourceRegistry()
    return _registry


class DataSourceRegistry:
    """统一入口。内置模式启动时自动填充，外挂模式由 API 动态填充。"""

    def __init__(self) -> None:
        self._providers: dict[str, DataSourceProvider] = {}
        self._cache: dict[str, DataSourceConfig] = {}
        self._credential = CredentialManager()

    def register_provider(self, name: str, provider: DataSourceProvider) -> None:
        """注册或替换一个数据源 Provider。

        Args:
            name: Provider 名称。
            provider: Provider 实例。

        Returns:
            无返回值。
        """
        logger.debug("注册数据源 Provider 入口", provider=name)
        self._providers[name] = provider
        logger.info("注册数据源 Provider 完成", provider=name)

    def get_provider(self, name: str) -> DataSourceProvider | None:
        """按名称获取已注册 Provider。

        Args:
            name: Provider 名称。

        Returns:
            Provider 实例；不存在返回 None。
        """
        provider = self._providers.get(name)
        logger.debug("获取数据源 Provider", provider=name, found=provider is not None)
        return provider

    def invalidate(self, name: str) -> bool:
        """清除数据源解析缓存但不删除 Provider 配置。

        Args:
            name: 数据源名称。

        Returns:
            存在并清除缓存时返回 True。
        """
        existed = self._cache.pop(name, None) is not None
        logger.info("数据源缓存失效", datasource=name, existed=existed)
        return existed

    async def unregister(self, name: str) -> bool:
        """从 Provider 和 Registry 删除数据源并释放引擎。

        Args:
            name: 数据源名称。

        Returns:
            数据源存在并删除时返回 True。
        """
        logger.debug("注销数据源入口", datasource=name)
        provider_found = False
        for provider in self._providers.values():
            config = await provider.lookup(name)
            if config is None:
                continue
            provider_found = True
            unregister = getattr(provider, "unregister", None)
            if unregister is not None:
                await unregister(name)
            break

        cached = self._cache.pop(name, None)
        if cached is not None and cached.engine is not None and not provider_found:
            dispose_result = cached.engine.dispose()
            if inspect.isawaitable(dispose_result):
                await dispose_result
            provider_found = True
        logger.info("注销数据源完成", datasource=name, removed=provider_found)
        return provider_found

    async def resolve(self, name: str) -> DataSourceConfig:
        """解析数据源 → 注入 engine → 缓存。

        schema 延迟加载：不在 resolve 时 introspect，
        交由 SchemaManager（ChromaDB 缓存优先）统一管理。
        避免每次服务重启都全量 INFORMATION_SCHEMA 查询。
        """
        logger.debug("解析数据源入口", datasource=name)
        if name in self._cache:
            logger.info("解析数据源命中缓存", datasource=name)
            return self._cache[name]

        for provider in self._providers.values():
            config = await provider.lookup(name)
            if config is None:
                continue

            if config.password:
                config.password = self._credential.resolve_env_ref(config.password)
                config.password = self._credential.decrypt(config.password)

            try:
                config.engine = await self._create_engine(config)

                if not await provider.test_connection(config):
                    raise ConnectionError(f"数据源 '{name}' 连接失败")

                # 不在 resolve 时做全量 introspect——
                # 如果 config 已有预设 schema 则保留，否则设为 None
                # SchemaManager.get_or_fetch_schema 会在需要时才从 ChromaDB 取
            except (ConnectionError, Exception) as e:
                logger.warning("数据源不可用，跳过", datasource=name, error=str(e)[:120])
                continue

            self._cache[name] = config
            logger.info("解析数据源完成", datasource=name, connected=True)
            return config

        raise DataSourceNotFoundError(name)

    async def _create_engine(self, ds: DataSourceConfig) -> AsyncEngine:
        """创建数据源引擎，并隔离可能阻塞事件循环的同步客户端初始化。

        Args:
            ds: 已解密凭证的数据源配置。

        Returns:
            SQLAlchemy 引擎或兼容的 ClickHouse 引擎适配器。
        """
        from urllib.parse import quote_plus
        logger.debug("创建数据源引擎入口", datasource=ds.name, dialect=ds.dialect)
        pwd = quote_plus(ds.password) if ds.password else ""
        sync_dialects = {"oracle", "mssql"}
        try:
            if ds.dialect == "clickhouse":
                engine = await asyncio.to_thread(self._build_engine, ds, pwd, sync_dialects)
            else:
                engine = self._build_engine(ds, pwd, sync_dialects)
            logger.info("创建数据源引擎完成", datasource=ds.name, dialect=ds.dialect)
            return engine
        except Exception as e:
            logger.error(
                "创建数据源引擎失败",
                datasource=ds.name,
                dialect=ds.dialect,
                error=str(e),
                exc_info=True,
            )
            raise ConnectionError(f"数据源 '{ds.name}' 方言驱动不可用: {ds.dialect} — {e}")

    def _build_engine(self, ds, pwd, sync_dialects):
        if ds.dialect == "sqlite":
            from sqlalchemy.pool import StaticPool

            path = ds.database or ":memory:"
            url = "sqlite+aiosqlite:///:memory:" if path == ":memory:" else f"sqlite+aiosqlite:///{path}"
            kwargs = {"pool_pre_ping": True, "echo": False}
            if path == ":memory:":
                kwargs["poolclass"] = StaticPool
            logger.info("创建 SQLite 引擎", datasource=ds.name, in_memory=path == ":memory:")
            return create_async_engine(url, **kwargs)
        if ds.dialect == "clickhouse":
            import clickhouse_connect
            import socket

            http_port = int(ds.extra_params.get("http_port") or (8123 if ds.port in (0, 9000) else ds.port))
            logger.debug(
                "创建 ClickHouse 客户端入口",
                datasource=ds.name,
                host=ds.host,
                configured_port=ds.port,
                http_port=http_port,
            )
            connect_timeout = max(1, int(ds.extra_params.get("connect_timeout", 5)))
            query_retries = max(0, int(ds.extra_params.get("query_retries", 0)))
            logger.info(
                "ClickHouse 建连参数",
                datasource=ds.name,
                connect_timeout=connect_timeout,
                query_retries=query_retries,
            )
            # 驱动强制保留一次 HTTP 重试，先做单次 TCP 探针以保证不可达地址按配置超时。
            logger.debug(
                "ClickHouse TCP 探针入口",
                datasource=ds.name,
                host=ds.host,
                port=http_port,
                timeout=connect_timeout,
            )
            with socket.create_connection((ds.host, http_port), timeout=connect_timeout):
                pass
            logger.info("ClickHouse TCP 探针完成", datasource=ds.name, success=True)
            client = clickhouse_connect.get_client(
                host=ds.host,
                port=http_port,
                username=ds.username,
                password=ds.password,
                database=ds.database or "default",
                connect_timeout=connect_timeout,
                query_retries=query_retries,
            )
            logger.info("创建 ClickHouse 客户端完成", datasource=ds.name)
            return _ClickHouseEngine(client)
        if ds.dialect in sync_dialects:
            from sqlalchemy import create_engine
            url_map = {
                "oracle": f"oracle+oracledb://{ds.username}:{pwd}@{ds.host}:{ds.port}/?service_name={ds.database}",
                "mssql": f"mssql+pymssql://{ds.username}:{pwd}@{ds.host}:{ds.port}/{ds.database}",
            }
            url = url_map.get(ds.dialect, "")
            return create_engine(
                url,
                pool_size=2, max_overflow=5,
                pool_pre_ping=True, pool_recycle=1800,
                echo=False,  # SQL 日志统一走 structlog，避免 echo=True 导致双份输出
            )
        scheme_map = {
            "mysql": "mysql+aiomysql",
            "postgres": "postgresql+asyncpg",
        }
        scheme = scheme_map.get(ds.dialect, "postgresql+asyncpg")
        # MySQL 需要 charset=utf8mb4 参数
        extra = "?charset=utf8mb4" if ds.dialect == "mysql" else ""
        url = f"{scheme}://{ds.username}:{pwd}@{ds.host}:{ds.port}/{ds.database}{extra}"
        pool_size = ds.extra_params.get("pool_size", 5)
        return create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=ds.extra_params.get("max_overflow", 10),
            pool_pre_ping=True,
            echo=False,  # SQL 日志统一走 structlog，避免 echo=True 导致双份输出
        )

    async def list_all(self) -> list[dict]:
        """列出 Provider 与缓存中的全部数据源。

        Returns:
            去重后的数据源摘要列表。
        """
        logger.debug("列出数据源入口", providers=len(self._providers), cached=len(self._cache))
        result = []
        seen: set[str] = set()
        for provider in self._providers.values():
            for ds in await provider.list_all():
                seen.add(ds.name)
                result.append({
                    "name": ds.name, "dialect": ds.dialect,
                    "mode": ds.mode, "host": ds.host,
                    "description": ds.description,
                    "connected": ds.name in self._cache,
                })
        # 包含直接注入缓存的 demo 数据源（不通过 Provider 注册）
        for name, config in self._cache.items():
            if name not in seen:
                result.append({
                    "name": config.name, "dialect": config.dialect,
                    "mode": config.mode, "host": config.host or "localhost",
                    "description": config.description or "",
                    "connected": True,
                })
        logger.info("列出数据源完成", count=len(result))
        return result

    async def resolve_or_none(self, name: str) -> DataSourceConfig | None:
        """解析数据源，未找到时返回 None。

        Args:
            name: 数据源名称。

        Returns:
            数据源配置或 None。
        """
        try:
            return await self.resolve(name)
        except Exception as exc:
            logger.warning("数据源解析回退为空", datasource=name, error=str(exc))
            return None
