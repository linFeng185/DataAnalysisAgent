"""DataSourceRegistry — 数据源统一注册与解析入口。"""

from __future__ import annotations

import asyncio
import inspect
import weakref
from typing import Any

from src.connectors import clickhouse as _clickhouse  # noqa: F401
from src.datasource.config import DataSourceConfig
from src.datasource.credential_manager import (
    CredentialManager,
)
from src.datasource.credential_manager import (
    describe_credential_reference as _credential_reference_kind,
)
from src.datasource.providers.base import DataSourceProvider
from src.exceptions import DataSourceNotFoundError
from src.logging_config import get_logger

logger = get_logger(__name__)

# 保留历史导入路径，同时由 clickhouse 模块完成连接器注册副作用。
_ClickHouseConnection = _clickhouse._ClickHouseConnection
_ClickHouseEngine = _clickhouse._ClickHouseEngine
_ClickHouseResult = _clickhouse._ClickHouseResult
_ClickHouseRow = _clickhouse._ClickHouseRow
_ELEVATED_DATABASE_ACCOUNTS = {
    "oracle": frozenset({"sys", "system"}),
    "mssql": frozenset({"sa"}),
}


class _StaleResolution(RuntimeError):
    """解析期间缓存被失效，需要重新读取 Provider 配置。"""


# 方法作用：从当前 AppContext 获取数据源 Registry，兼容既有调用入口。
# Args: 无。
# Returns: 当前应用独享的 DataSourceRegistry 实例。
def get_registry() -> "DataSourceRegistry":
    """获取当前应用的数据源 Registry。

    Returns:
        当前 AppContext 的 DataSourceRegistry 实例。
    """
    from src.app_context import get_app_context

    logger.debug("获取数据源 Registry 入口")
    result = get_app_context().get_or_create(
        "datasource_registry",
        DataSourceRegistry,
    )
    logger.debug("获取数据源 Registry 完成")
    return result


class DataSourceRegistry:
    """统一入口。内置模式启动时自动填充，外挂模式由 API 动态填充。"""

    def __init__(self) -> None:
        self._providers: dict[str, DataSourceProvider] = {}
        self._cache: dict[tuple[int, str], DataSourceConfig] = {}
        # 解析锁只在有协程持有或等待时保留，避免动态数据源名称造成永久增长。
        self._resolve_locks: weakref.WeakValueDictionary[
            tuple[int, str], asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._resolution_generations: dict[tuple[int, str], int] = {}
        self._pending_release_tasks: set[asyncio.Task[Any]] = set()
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

    def invalidate(self, name: str, *, tenant_id: int | None = None) -> bool:
        """清除数据源解析缓存但不删除 Provider 配置。

        Args:
            name: 数据源名称。

        Returns:
            存在并清除缓存时返回 True。
        """
        isolation_enabled = self._isolation_enabled()
        requested_tenant = self._effective_tenant_id(tenant_id)
        effective_tenant = requested_tenant if isolation_enabled else None
        self._bump_resolution_generations(
            name,
            requested_tenant=requested_tenant,
            effective_tenant=effective_tenant,
        )
        keys = [
            key for key, config in self._cache.items()
            if self._cache_key_matches(key, config, name, effective_tenant)
        ]
        existed = bool(keys)
        released: set[int] = set()
        for key in keys:
            config = self._cache.pop(key, None)
            if config is not None and id(config) not in released:
                released.add(id(config))
                self._release_runtime_config(config, name)
        self._cleanup_resolution_states(name, effective_tenant)
        logger.info(
            "数据源缓存失效",
            datasource=name,
            existed=existed,
            tenant_id=effective_tenant,
        )
        return existed

    # 方法作用：标记指定数据源的进行中解析结果已过期，阻止旧配置写回缓存。
    # Args: self - Registry 实例；name - 数据源名称；requested_tenant - 当前请求租户；effective_tenant - 隔离策略下的匹配租户。
    # Returns: 无返回值。
    def _bump_resolution_generations(
        self,
        name: str,
        *,
        requested_tenant: int,
        effective_tenant: int | None,
    ) -> None:
        candidate_keys: set[tuple[int, str]] = set()
        for key in (*self._cache.keys(), *self._resolve_locks.keys()):
            if isinstance(key, tuple) and len(key) == 2:
                if effective_tenant is None or int(key[0]) == int(effective_tenant):
                    if str(key[1]) == name:
                        candidate_keys.add((int(key[0]), str(key[1])))
                continue
            config = self._cache.get(key)
            if config is not None and self._cache_key_matches(
                key,
                config,
                name,
                effective_tenant,
            ):
                candidate_keys.add((int(getattr(config, "tenant_id", 1) or 1), name))
        # 即使当前尚无缓存，也要覆盖刚创建的解析锁；后续解析会读取该代次。
        candidate_keys.add((requested_tenant, name))
        for key in candidate_keys:
            self._resolution_generations[key] = (
                self._resolution_generations.get(key, 0) + 1
            )
        logger.info(
            "数据源解析代次递增",
            datasource=name,
            tenant_id=effective_tenant,
            key_count=len(candidate_keys),
        )

    # 方法作用：找出指定数据源仍有协调状态的租户复合键。
    # Args: self - Registry 实例；name - 数据源名称；tenant_id - 租户过滤，None 表示全部租户。
    # Returns: 需要尝试回收的租户和名称复合键集合。
    def _resolution_keys_for_name(
        self,
        name: str,
        tenant_id: int | None,
    ) -> set[tuple[int, str]]:
        keys: set[tuple[int, str]] = set()
        raw_keys = (
            tuple(self._cache.keys())
            + tuple(self._resolve_locks.keys())
            + tuple(self._resolution_generations.keys())
        )
        for raw_key in raw_keys:
            if isinstance(raw_key, tuple) and len(raw_key) == 2:
                key = (int(raw_key[0]), str(raw_key[1]))
                if key[1] == name and (tenant_id is None or key[0] == int(tenant_id)):
                    keys.add(key)
                continue
            config = self._cache.get(raw_key)
            if config is None or str(raw_key) != name:
                continue
            config_tenant = int(getattr(config, "tenant_id", 1) or 1)
            if tenant_id is None or config_tenant == int(tenant_id):
                keys.add((config_tenant, name))
        return keys

    # 方法作用：在没有进行中解析时回收指定复合键的代次元数据。
    # Args: self - Registry 实例；key - 租户和数据源名称复合键。
    # Returns: 无返回值；仍有持有者时保留代次以检测失效竞态。
    def _cleanup_resolution_state(self, key: tuple[int, str]) -> None:
        lock = self._resolve_locks.get(key)
        if lock is not None and lock.locked():
            return
        if self._resolution_generations.pop(key, None) is not None:
            logger.debug(
                "数据源解析代次回收完成",
                datasource=key[1],
                tenant_id=key[0],
            )

    # 方法作用：批量回收指定数据源已结束的解析协调状态。
    # Args: self - Registry 实例；name - 数据源名称；tenant_id - 租户过滤，None 表示全部租户。
    # Returns: 无返回值。
    def _cleanup_resolution_states(self, name: str, tenant_id: int | None) -> None:
        for key in self._resolution_keys_for_name(name, tenant_id):
            self._cleanup_resolution_state(key)

    # 方法作用：从失效缓存中摘除并释放旧数据源的 Connector 或 Engine。
    # Args: self - Registry 实例；config - 待释放的运行时配置；datasource - 数据源名称。
    # Returns: 无返回值，异步释放任务由 Registry 统一跟踪。
    def _release_runtime_config(self, config: DataSourceConfig, datasource: str) -> None:
        connector = getattr(config, "connector", None)
        engine = getattr(config, "engine", None)
        # 先清空引用，避免替换/失效并发时再次释放同一资源。
        config.connector = None
        config.engine = None

        resource = connector if connector is not None else engine
        action = "close" if connector is not None else "dispose"
        if resource is None:
            logger.debug("数据源运行时资源释放跳过", datasource=datasource, reason="无资源")
            return
        method = getattr(resource, action, None)
        if method is None:
            logger.warning(
                "数据源运行时资源缺少释放方法",
                datasource=datasource,
                action=action,
                resource_type=type(resource).__name__,
            )
            return
        try:
            result = method()
        except Exception as exc:
            logger.error(
                "数据源运行时资源同步释放失败",
                datasource=datasource,
                action=action,
                error=str(exc),
                exc_info=True,
            )
            return
        if inspect.isawaitable(result):
            self._schedule_runtime_release(result, datasource, action)
        else:
            logger.info("数据源运行时资源释放完成", datasource=datasource, action=action)

    # 方法作用：在合适的事件循环中执行异步资源释放，并隔离清理异常。
    # Args: self - Registry 实例；awaitable - 异步释放结果；datasource - 数据源名称；action - 释放动作。
    # Returns: 无返回值，任务完成后从待处理集合移除。
    def _schedule_runtime_release(
        self,
        awaitable: Any,
        datasource: str,
        action: str,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(self._await_runtime_release(awaitable, datasource, action))
            except Exception as exc:
                logger.error(
                    "数据源运行时资源异步释放调度失败",
                    datasource=datasource,
                    action=action,
                    error=str(exc),
                    exc_info=True,
                )
            return
        try:
            task = loop.create_task(
                self._await_runtime_release(awaitable, datasource, action),
            )
        except Exception as exc:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            logger.error(
                "数据源运行时资源异步释放调度失败",
                datasource=datasource,
                action=action,
                error=str(exc),
                exc_info=True,
            )
            return
        self._pending_release_tasks.add(task)
        task.add_done_callback(self._pending_release_tasks.discard)

    # 方法作用：等待异步资源释放并记录可恢复的清理故障。
    # Args: self - Registry 实例；awaitable - 异步释放结果；datasource - 数据源名称；action - 释放动作。
    # Returns: 无返回值。
    async def _await_runtime_release(
        self,
        awaitable: Any,
        datasource: str,
        action: str,
    ) -> None:
        try:
            await awaitable
        except asyncio.CancelledError:
            logger.warning(
                "数据源运行时资源释放任务被取消",
                datasource=datasource,
                action=action,
            )
        except Exception as exc:
            logger.error(
                "数据源运行时资源异步释放失败",
                datasource=datasource,
                action=action,
                error=str(exc),
                exc_info=True,
            )
        else:
            logger.info("数据源运行时资源释放完成", datasource=datasource, action=action)

    # 方法作用：通过公共边界注册或替换已构建的数据源配置。
    # Args: config - 已完成 Engine/Schema 初始化的数据源配置；replace - 是否允许覆盖同名配置。
    # Returns: 注册后的原配置对象。
    def register_config(
        self,
        config: DataSourceConfig,
        *,
        replace: bool = True,
    ) -> DataSourceConfig:
        logger.debug(
            "注册数据源配置入口",
            datasource=config.name,
            replace=replace,
        )
        if not config.name.strip():
            logger.error("注册数据源配置失败", reason="数据源名称为空")
            raise ValueError("数据源名称不能为空")
        key = (int(config.tenant_id), config.name)
        previous = self._cache.get(key)
        if previous is not None and not replace:
            logger.warning("注册数据源配置拒绝", datasource=config.name, reason="已存在")
            raise ValueError(f"数据源配置已存在: {config.name}")
        isolation_enabled = self._isolation_enabled()
        self._bump_resolution_generations(
            config.name,
            requested_tenant=key[0],
            effective_tenant=key[0] if isolation_enabled else None,
        )
        self._cache[key] = config
        if previous is not None and previous is not config:
            self._release_runtime_config(previous, config.name)
        self._cleanup_resolution_states(
            config.name,
            key[0] if isolation_enabled else None,
        )
        logger.info("注册数据源配置完成", datasource=config.name)
        return config

    async def unregister(self, name: str, *, tenant_id: int | None = None) -> bool:
        """从 Provider 和 Registry 删除数据源并释放引擎。

        Args:
            name: 数据源名称。

        Returns:
            数据源存在并删除时返回 True。
        """
        logger.debug("注销数据源入口", datasource=name)
        isolation_enabled = self._isolation_enabled()
        requested_tenant = self._effective_tenant_id(tenant_id)
        lock_key = (requested_tenant, name)
        lock = self._resolve_locks.setdefault(lock_key, asyncio.Lock())
        try:
            async with lock:
                return await self._unregister_locked(name, tenant_id=tenant_id)
        finally:
            self._cleanup_resolution_states(
                name,
                requested_tenant if isolation_enabled else None,
            )

    # 方法作用：在解析锁内删除 Provider 配置、缓存配置并释放运行时资源。
    # Args: self - Registry 实例；name - 数据源名称；tenant_id - 可选租户 ID。
    # Returns: 数据源存在并删除时返回 True。
    async def _unregister_locked(self, name: str, *, tenant_id: int | None = None) -> bool:
        isolation_enabled = self._isolation_enabled()
        effective_tenant = self._effective_tenant_id(tenant_id) if isolation_enabled else None
        self._bump_resolution_generations(
            name,
            requested_tenant=self._effective_tenant_id(tenant_id),
            effective_tenant=effective_tenant,
        )
        provider_found = False
        for provider in self._providers.values():
            config = await self._provider_lookup(provider, name, effective_tenant)
            if config is None:
                continue
            provider_found = True
            unregister = getattr(provider, "unregister", None)
            if unregister is not None:
                await self._call_with_optional_tenant(
                    unregister,
                    name,
                    tenant_id=effective_tenant,
                )
            break

        cached_keys = [
            key for key, config in self._cache.items()
            if self._cache_key_matches(key, config, name, effective_tenant)
        ]
        cached = None
        for key in cached_keys:
            cached = self._cache.pop(key, None) or cached
        if cached is not None:
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

    async def resolve(self, name: str, *, tenant_id: int | None = None) -> DataSourceConfig:
        """解析数据源 → 注入 engine → 缓存。

        schema 延迟加载：不在 resolve 时 introspect，
        交由 SchemaManager（ChromaDB 缓存优先）统一管理。
        避免每次服务重启都全量 INFORMATION_SCHEMA 查询。
        """
        logger.debug("解析数据源入口", datasource=name)
        effective_tenant = self._effective_tenant_id(tenant_id)
        cache_key = (effective_tenant, name)
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.info("解析数据源命中缓存", datasource=name)
            return cached

        # 同一租户和名称只允许一个协程创建连接池，其他协程在锁内二次检查缓存。
        lock = self._resolve_locks.setdefault(cache_key, asyncio.Lock())
        try:
            async with lock:
                while True:
                    cached = self._cache.get(cache_key)
                    if cached is not None:
                        logger.info("解析数据源等待后命中缓存", datasource=name)
                        return cached
                    generation = self._resolution_generations.get(cache_key, 0)
                    try:
                        return await self._resolve_uncached(
                            name,
                            effective_tenant,
                            cache_key,
                            generation,
                        )
                    except _StaleResolution:
                        logger.info(
                            "解析数据源检测到失效代次，重新解析",
                            datasource=name,
                            tenant_id=effective_tenant,
                        )
        finally:
            self._cleanup_resolution_state(cache_key)

    # 方法作用：在解析锁内查找 Provider、创建引擎并写入复合键缓存。
    # Args: self - Registry 实例；name - 数据源名称；tenant_id - 有效租户；cache_key - 租户和名称复合键。
    # Returns: 已连接的数据源配置；所有 Provider 均不可用时抛出 DataSourceNotFoundError。
    async def _resolve_uncached(
        self,
        name: str,
        tenant_id: int,
        cache_key: tuple[int, str],
        generation: int,
    ) -> DataSourceConfig:
        # 迁移期旧键只有归属租户一致时才允许命中，避免绕过复合键隔离。
        if name in self._cache:
            legacy_config = self._cache[name]
            legacy_tenant = int(getattr(legacy_config, "tenant_id", 1) or 1)
            legacy_allowed = not self._isolation_enabled() or legacy_tenant == tenant_id
            logger.info(
                "旧格式数据源缓存租户校验探针",
                datasource=name,
                requested_tenant=tenant_id,
                cached_tenant=legacy_tenant,
                allowed=legacy_allowed,
            )
            if legacy_allowed:
                logger.info("命中旧格式数据源缓存键", datasource=name)
                return legacy_config

        for provider in self._providers.values():
            config = await self._provider_lookup(provider, name, tenant_id)
            if config is None:
                continue

            dialect = config.dialect.strip().casefold()
            username = config.username.strip()
            elevated_accounts = _ELEVATED_DATABASE_ACCOUNTS.get(dialect, frozenset())
            if username.casefold() in elevated_accounts:
                logger.warning(
                    "数据源使用高权限数据库账号，继续连接",
                    datasource=config.name,
                    dialect=config.dialect,
                    username=username,
                    protection="应用层只读 SQL 校验仍启用",
                )

            if config.password:
                logger.info(
                    "数据源凭证解密边界",
                    datasource=config.name,
                    credential_format=_credential_reference_kind(config.password),
                    phase="before",
                )
                config.password = self._credential.resolve_env_ref(config.password)
                config.password = self._credential.decrypt(config.password)
                logger.info(
                    "数据源凭证解密边界",
                    datasource=config.name,
                    credential_configured=bool(config.password),
                    phase="after",
                )

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
                elif config.engine is not None:
                    dispose_result = config.engine.dispose()
                    if inspect.isawaitable(dispose_result):
                        await dispose_result
                config.engine = None
                continue

            if self._resolution_generations.get(cache_key, 0) != generation:
                logger.warning(
                    "解析数据源结果已失效，丢弃旧运行时资源",
                    datasource=name,
                    tenant_id=tenant_id,
                    started_generation=generation,
                    current_generation=self._resolution_generations.get(cache_key, 0),
                )
                self._release_runtime_config(config, name)
                raise _StaleResolution
            self._cache[cache_key] = config
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

    async def list_all(self, *, tenant_id: int | None = None) -> list[dict]:
        """列出 Provider 与缓存中的全部数据源。

        Returns:
            去重后的数据源摘要列表。
        """
        logger.debug("列出数据源入口", providers=len(self._providers), cached=len(self._cache))
        isolation_enabled = self._isolation_enabled()
        effective_tenant = self._effective_tenant_id(tenant_id) if isolation_enabled else None
        result = []
        seen: set[str] = set()
        for provider in self._providers.values():
            provider_items = await self._call_with_optional_tenant(
                provider.list_all,
                tenant_id=effective_tenant,
            )
            for ds in provider_items:
                seen.add(ds.name)
                result.append({
                    "name": ds.name, "dialect": ds.dialect,
                    "version": ds.version,
                    "mode": ds.mode, "host": ds.host,
                    "port": ds.port, "database": ds.database,
                    "username": ds.username,
                    "description": ds.description,
                    "schema": ds.extra_params.get("schema", ""),
                    "tablespace": ds.extra_params.get("tablespace", ""),
                    "service_name": ds.extra_params.get("service_name", ""),
                    "instance": ds.extra_params.get("instance", ""),
                    "file_path": ds.database if ds.dialect == "sqlite" else "",
                    "connected": (int(ds.tenant_id), ds.name) in self._cache,
                })
        # 包含直接注入缓存的 demo 数据源（不通过 Provider 注册）
        for raw_key, config in self._cache.items():
            if isinstance(raw_key, tuple):
                source_tenant, name = raw_key
            else:
                source_tenant, name = int(getattr(config, "tenant_id", 1) or 1), str(raw_key)
            if isolation_enabled and source_tenant != effective_tenant:
                continue
            if name not in seen:
                result.append({
                    "name": config.name, "dialect": config.dialect,
                    "version": config.version,
                    "mode": config.mode, "host": config.host or "localhost",
                    "port": config.port, "database": config.database,
                    "username": config.username,
                    "description": config.description or "",
                    "schema": config.extra_params.get("schema", ""),
                    "tablespace": config.extra_params.get("tablespace", ""),
                    "service_name": config.extra_params.get("service_name", ""),
                    "instance": config.extra_params.get("instance", ""),
                    "file_path": config.database if config.dialect == "sqlite" else "",
                    "connected": True,
                })
        logger.info("列出数据源完成", count=len(result))
        return result

    # 方法作用：为系统后台任务枚举当前 Registry 中实际存在数据源的全部租户。
    # Args: self - DataSourceRegistry 实例。
    # Returns: 去重并升序排列的租户 ID。
    async def list_tenant_ids(self) -> list[int]:
        logger.debug("枚举数据源租户入口", providers=len(self._providers), cached=len(self._cache))
        tenant_ids = {
            int(getattr(config, "tenant_id", 1) or 1)
            for config in self._cache.values()
        }
        for provider in self._providers.values():
            list_tenant_ids = getattr(provider, "list_tenant_ids", None)
            if list_tenant_ids is not None:
                provider_tenant_ids = await list_tenant_ids()
                tenant_ids.update(int(tenant_id) for tenant_id in provider_tenant_ids)
                continue
            provider_items = await self._call_with_optional_tenant(
                provider.list_all,
                tenant_id=None,
            )
            tenant_ids.update(
                int(getattr(config, "tenant_id", 1) or 1)
                for config in provider_items
            )
        result = sorted(tenant_id for tenant_id in tenant_ids if tenant_id > 0)
        logger.info("枚举数据源租户完成", tenant_count=len(result))
        return result

    @staticmethod
    async def _provider_lookup(provider: DataSourceProvider, name: str, tenant_id: int | None):
        return await DataSourceRegistry._call_with_optional_tenant(
            provider.lookup,
            name,
            tenant_id=tenant_id,
        )

    # 方法作用：兼容旧 Provider 的可选租户参数，同时不吞掉方法内部 TypeError。
    # Args: method - Provider 异步方法；args - 位置参数；tenant_id - 租户 ID。
    # Returns: Provider 方法的异步返回值。
    @staticmethod
    async def _call_with_optional_tenant(
        method: Any,
        *args: Any,
        tenant_id: int | None,
    ) -> Any:
        try:
            signature = inspect.signature(method)
            parameters = signature.parameters.values()
            tenant_parameter = signature.parameters.get("tenant_id")
            supports_positional = bool(
                tenant_parameter
                and tenant_parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            )
            supports_keyword = (
                bool(tenant_parameter) and not supports_positional
            ) or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_positional = False
            supports_keyword = None

        if supports_positional:
            result = method(*args, tenant_id)
            return await result if inspect.isawaitable(result) else result
        if supports_keyword is False:
            result = method(*args)
            return await result if inspect.isawaitable(result) else result
        try:
            result = method(*args, tenant_id=tenant_id)
        except TypeError as exc:
            # 只有运行时明确报告未知关键字时才回退，避免重复执行有副作用的 Provider 方法。
            message = str(exc).lower()
            if supports_keyword is True or "unexpected keyword argument" not in message:
                raise
            result = method(*args)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _cache_key_matches(
        key: object,
        config: DataSourceConfig,
        name: str,
        tenant_id: int | None,
    ) -> bool:
        if isinstance(key, tuple) and len(key) == 2:
            tenant_matches = tenant_id is None or int(key[0]) == int(tenant_id)
            return tenant_matches and str(key[1]) == name
        config_tenant = int(getattr(config, "tenant_id", 1) or 1)
        tenant_matches = tenant_id is None or config_tenant == int(tenant_id)
        return tenant_matches and str(key) == name

    @staticmethod
    def _isolation_enabled() -> bool:
        try:
            from src.app_context import get_tenant_policy

            return bool(get_tenant_policy().datasource_isolation_enabled)
        except Exception:
            logger.debug("获取数据源隔离策略失败，使用兼容模式", exc_info=True)
            return False

    @staticmethod
    def _effective_tenant_id(tenant_id: int | None) -> int:
        if tenant_id is not None:
            return int(tenant_id)
        try:
            from src.api.auth import get_current_tenant_id

            return int(get_current_tenant_id())
        except Exception:
            logger.debug("获取当前租户失败，使用默认租户", exc_info=True)
            return 1

    async def resolve_or_none(
        self,
        name: str,
        *,
        tenant_id: int | None = None,
    ) -> DataSourceConfig | None:
        """解析数据源，未找到时返回 None。

        Args:
            name: 数据源名称。

        Returns:
            数据源配置或 None。
        """
        try:
            return await self.resolve(name, tenant_id=tenant_id)
        except DataSourceNotFoundError:
            logger.info("数据源解析回退为空", datasource=name, reason="not_found")
            return None
        except Exception as exc:
            logger.error(
                "数据源解析失败",
                datasource=name,
                error=str(exc),
                exc_info=True,
            )
            raise
