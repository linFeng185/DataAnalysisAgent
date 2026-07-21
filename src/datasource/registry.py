"""DataSourceRegistry — 数据源统一注册与解析入口。"""

from __future__ import annotations

import inspect
from typing import Any

from src.connectors.clickhouse import (
    _ClickHouseConnection,
    _ClickHouseEngine,
    _ClickHouseResult,
    _ClickHouseRow,
)
from src.datasource.config import DataSourceConfig
from src.datasource.credential_manager import CredentialManager
from src.datasource.providers.base import DataSourceProvider
from src.exceptions import DataSourceNotFoundError
from src.logging_config import get_logger

logger = get_logger(__name__)


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
        if cached is not None and not provider_found:
            if cached.connector is not None:
                await cached.connector.close()
                cached.connector = None
                cached.engine = None
            elif cached.engine is not None:
                dispose_result = cached.engine.dispose()
                if inspect.isawaitable(dispose_result):
                    await dispose_result
                cached.engine = None
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
            except Exception as e:
                logger.warning("数据源不可用，跳过", datasource=name, error=str(e)[:120])
                if config.connector is not None:
                    await config.connector.close()
                    config.connector = None
                    config.engine = None
                continue

            self._cache[name] = config
            logger.info("解析数据源完成", datasource=name, connected=True)
            return config

        raise DataSourceNotFoundError(name)

    async def _create_engine(self, ds: DataSourceConfig) -> Any:
        """委托方言连接器创建并缓存运行时引擎。

        Args:
            ds: 已解密凭证的数据源配置。

        Returns:
            SQLAlchemy 引擎或方言兼容引擎适配器。
        """
        logger.debug("创建数据源引擎入口", datasource=ds.name, dialect=ds.dialect)
        try:
            import src.connectors.registry as connector_registry

            connector = connector_registry.create_connector(ds)
            engine = await connector.create_engine()
            ds.connector = connector
            logger.info("创建数据源引擎完成", datasource=ds.name, dialect=ds.dialect)
            return engine
        except Exception as exc:
            logger.error(
                "创建数据源引擎失败",
                datasource=ds.name,
                dialect=ds.dialect,
                error=str(exc),
                exc_info=True,
            )
            raise ConnectionError(
                f"数据源 '{ds.name}' 方言驱动不可用: {ds.dialect} — {exc}"
            ) from exc

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
