"""数据源 Registry 并发解析回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.datasource.config import DataSourceConfig
from src.datasource.registry import DataSourceRegistry


class TestDataSourceRegistryConcurrency:
    """覆盖功能 2.1.2：同一租户的并发解析只创建一个运行时引擎。"""

    # 方法作用：验证并发请求共享同一个解析任务，避免重复创建连接池。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_resolve_deduplicates_concurrent_engine_creation(self) -> None:
        """同一数据源的两个并发请求必须返回同一个缓存配置。"""
        # Arrange
        class _Provider:
            def __init__(self) -> None:
                self.lookup_count = 0

            async def lookup(self, name: str, *, tenant_id: int | None = None):
                self.lookup_count += 1
                await asyncio.sleep(0)
                return DataSourceConfig(
                    name=name,
                    dialect="sqlite",
                    mode="external",
                    tenant_id=tenant_id or 1,
                )

            async def test_connection(self, _config: DataSourceConfig) -> bool:
                await asyncio.sleep(0)
                return True

        registry = DataSourceRegistry()
        provider = _Provider()
        registry.register_provider("test", provider)
        created_engines: list[object] = []

        async def _create_engine(_config: DataSourceConfig) -> object:
            await asyncio.sleep(0.02)
            engine = object()
            created_engines.append(engine)
            return engine

        registry._create_engine = _create_engine  # type: ignore[method-assign]

        # Act
        first, second = await asyncio.gather(
            registry.resolve("sales", tenant_id=7),
            registry.resolve("sales", tenant_id=7),
        )

        # Assert
        assert first is second
        assert provider.lookup_count == 1
        assert len(created_engines) == 1
        assert registry._cache[(7, "sales")] is first  # noqa: SLF001

    # 方法作用：验证注销请求等待进行中的解析并最终移除刚创建的运行时配置。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unregister_does_not_allow_inflight_resolve_to_resurrect_cache(self) -> None:
        """删除与首次连接建立并发时，数据源不得被解析任务复活。"""
        # Arrange
        started = asyncio.Event()
        release = asyncio.Event()

        class _Provider:
            def __init__(self) -> None:
                self.source = DataSourceConfig(
                    name="sales",
                    dialect="sqlite",
                    mode="external",
                    tenant_id=7,
                )

            async def lookup(self, name: str, *, tenant_id: int | None = None):
                if name == self.source.name and tenant_id == 7:
                    return self.source
                return None

            async def test_connection(self, _config: DataSourceConfig) -> bool:
                return True

            async def unregister(self, name: str, *, tenant_id: int | None = None) -> bool:
                if name == self.source.name and tenant_id == 7:
                    self.source = DataSourceConfig(
                        name="removed",
                        dialect="sqlite",
                        mode="external",
                        tenant_id=7,
                    )
                    return True
                return False

        registry = DataSourceRegistry()
        provider = _Provider()
        registry.register_provider("test", provider)

        async def _create_engine(_config: DataSourceConfig) -> object:
            started.set()
            await release.wait()
            return SimpleNamespace(dispose=lambda: None)

        registry._create_engine = _create_engine  # type: ignore[method-assign]

        # Act
        resolving = asyncio.create_task(registry.resolve("sales", tenant_id=7))
        await started.wait()
        removing = asyncio.create_task(registry.unregister("sales", tenant_id=7))
        await asyncio.sleep(0)
        release.set()
        await resolving
        removed = await removing

        # Assert
        assert removed is True
        assert (7, "sales") not in registry._cache  # noqa: SLF001

    # 方法作用：验证同步 invalidate 与首次解析并发时不会把失效前的配置重新写回缓存。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_invalidate_does_not_allow_inflight_resolve_to_resurrect_cache(self) -> None:
        """更新操作在建连期间失效缓存后，旧解析结果不得复活。"""
        # Arrange
        started = asyncio.Event()
        release = asyncio.Event()

        class _Provider:
            def __init__(self) -> None:
                self.lookup_count = 0

            async def lookup(self, name: str, *, tenant_id: int | None = None):
                self.lookup_count += 1
                return DataSourceConfig(
                    name=name,
                    dialect="sqlite",
                    mode="embedded",
                    tenant_id=tenant_id or 1,
                    extra_params={"version": self.lookup_count},
                )

            async def test_connection(self, _config: DataSourceConfig) -> bool:
                return True

        registry = DataSourceRegistry()
        provider = _Provider()
        registry.register_provider("test", provider)
        registry._isolation_enabled = lambda: True  # type: ignore[method-assign]

        async def _create_engine(_config: DataSourceConfig) -> object:
            started.set()
            await release.wait()
            return SimpleNamespace(dispose=lambda: None)

        registry._create_engine = _create_engine  # type: ignore[method-assign]

        # Act
        resolving = asyncio.create_task(registry.resolve("sales", tenant_id=7))
        await started.wait()
        invalidated = registry.invalidate("sales", tenant_id=7)
        release.set()
        await resolving

        # Assert
        assert invalidated is False
        assert provider.lookup_count == 2
        assert (7, "sales") in registry._cache  # noqa: SLF001
        assert registry._cache[(7, "sales")].extra_params["version"] == 2  # noqa: SLF001

    # 方法作用：验证 Provider 没有 unregister 方法时 Registry 仍释放已缓存引擎。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unregister_releases_cached_engine_without_provider_hook(self) -> None:
        """内置 Provider 删除路径不能遗留连接池。"""
        # Arrange
        class _Provider:
            async def lookup(self, name: str, *, tenant_id: int | None = None):
                if name == "sales" and tenant_id == 7:
                    return DataSourceConfig(
                        name="sales",
                        dialect="sqlite",
                        mode="embedded",
                        tenant_id=7,
                    )
                return None

        dispose_calls: list[bool] = []

        async def _dispose() -> None:
            dispose_calls.append(True)

        engine = SimpleNamespace(dispose=_dispose)
        config = DataSourceConfig(
            name="sales",
            dialect="sqlite",
            mode="embedded",
            tenant_id=7,
            engine=engine,
        )
        registry = DataSourceRegistry()
        registry.register_provider("embedded", _Provider())
        registry.register_config(config)

        # Act
        removed = await registry.unregister("sales", tenant_id=7)

        # Assert
        assert removed is True
        assert dispose_calls == [True]
        assert config.engine is None
        assert (7, "sales") not in registry._cache  # noqa: SLF001

    # 方法作用：验证同步 invalidate 会释放缓存引擎，并清空运行时引用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_invalidate_releases_cached_engine(self) -> None:
        """更新数据源后失效旧缓存不得遗留 Engine。"""
        # Arrange
        dispose_calls: list[bool] = []

        def _dispose() -> None:
            dispose_calls.append(True)

        engine = SimpleNamespace(dispose=_dispose)
        config = DataSourceConfig(
            name="sales",
            dialect="sqlite",
            mode="embedded",
            tenant_id=7,
            engine=engine,
        )
        registry = DataSourceRegistry()
        registry.register_config(config)

        # Act
        invalidated = registry.invalidate("sales", tenant_id=7)

        # Assert
        assert invalidated is True
        assert dispose_calls == [True]
        assert config.engine is None
        assert (7, "sales") not in registry._cache  # noqa: SLF001

    # 方法作用：验证异步 Connector 关闭会在当前事件循环中完成且不泄漏引用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_invalidate_schedules_async_connector_close(self) -> None:
        """外挂数据源更新后应异步释放旧 Connector。"""
        # Arrange
        close_calls: list[bool] = []

        async def _close() -> None:
            close_calls.append(True)

        connector = SimpleNamespace(close=_close)
        config = DataSourceConfig(
            name="sales",
            dialect="sqlite",
            mode="external",
            tenant_id=7,
            connector=connector,
            engine=SimpleNamespace(dispose=lambda: None),
        )
        registry = DataSourceRegistry()
        registry.register_config(config)

        # Act
        invalidated = registry.invalidate("sales", tenant_id=7)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Assert
        assert invalidated is True
        assert close_calls == [True]
        assert config.connector is None
        assert config.engine is None
        assert (7, "sales") not in registry._cache  # noqa: SLF001

    # 方法作用：验证异步资源关闭失败时 invalidate 仍完成缓存失效，不阻断更新流程。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_invalidate_tolerates_async_close_failure(self) -> None:
        """旧连接清理失败不应让新配置更新请求失败。"""
        # Arrange
        async def _close() -> None:
            raise RuntimeError("close failed")

        config = DataSourceConfig(
            name="sales",
            dialect="sqlite",
            mode="external",
            tenant_id=7,
            connector=SimpleNamespace(close=_close),
        )
        registry = DataSourceRegistry()
        registry.register_config(config)

        # Act
        invalidated = registry.invalidate("sales", tenant_id=7)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Assert
        assert invalidated is True
        assert (7, "sales") not in registry._cache  # noqa: SLF001

    # 方法作用：验证替换同名配置时会释放被覆盖的旧运行时引擎。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_register_config_releases_replaced_engine(self) -> None:
        """replace=True 不应遗留旧 Engine。"""
        # Arrange
        dispose_calls: list[bool] = []

        def _dispose() -> None:
            dispose_calls.append(True)

        old_config = DataSourceConfig(
            name="sales",
            dialect="sqlite",
            mode="embedded",
            tenant_id=7,
            engine=SimpleNamespace(dispose=_dispose),
        )
        new_config = DataSourceConfig(
            name="sales",
            dialect="sqlite",
            mode="embedded",
            tenant_id=7,
            engine=SimpleNamespace(dispose=lambda: None),
        )
        registry = DataSourceRegistry()
        registry.register_config(old_config)

        # Act
        registry.register_config(new_config, replace=True)

        # Assert
        assert dispose_calls == [True]
        assert registry._cache[(7, "sales")] is new_config  # noqa: SLF001

    # 方法作用：验证 Provider 内部 TypeError 不会被兼容分支重复调用或吞掉。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_provider_internal_type_error_is_not_retried(self) -> None:
        """只有签名不支持 tenant_id 时才允许回退调用。"""
        # Arrange
        class _BrokenProvider:
            def __init__(self) -> None:
                self.calls = 0

            async def lookup(self, _name: str, *, tenant_id: int | None = None):
                del tenant_id
                self.calls += 1
                raise TypeError("provider body is broken")

        provider = _BrokenProvider()
        registry = DataSourceRegistry()
        registry.register_provider("broken", provider)

        # Act / Assert
        with pytest.raises(TypeError, match="provider body is broken"):
            await registry.resolve("sales", tenant_id=7)
        assert provider.calls == 1

    # 方法作用：验证没有 tenant_id 参数的历史 Provider 仍能通过明确兼容路径工作。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_legacy_provider_without_tenant_keyword_still_works(self) -> None:
        """迁移期旧 Provider 不应因新增租户参数而不可用。"""
        # Arrange
        class _LegacyProvider:
            async def lookup(self, name: str):
                return DataSourceConfig(name=name, dialect="sqlite", mode="embedded")

            async def test_connection(self, _config: DataSourceConfig) -> bool:
                return True

        registry = DataSourceRegistry()
        registry.register_provider("legacy", _LegacyProvider())
        registry._create_engine = lambda _config: asyncio.sleep(0, result=SimpleNamespace())  # type: ignore[method-assign]

        # Act
        resolved = await registry.resolve("sales", tenant_id=7)

        # Assert
        assert resolved.name == "sales"

    # 方法作用：验证无法读取 Provider 签名时也不重试方法体内部的 TypeError。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unknown_provider_signature_does_not_retry_internal_type_error(self) -> None:
        """Provider 方法体异常必须原样传播且只执行一次。"""
        # Arrange
        class _OpaqueLookup:
            __signature__ = object()

            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, *_args, **_kwargs):
                self.calls += 1
                raise TypeError("unexpected keyword argument from provider body")

        class _Provider:
            def __init__(self) -> None:
                self.lookup = _OpaqueLookup()

        provider = _Provider()
        registry = DataSourceRegistry()
        registry.register_provider("opaque", provider)

        # Act / Assert
        with pytest.raises(TypeError, match="from provider body"):
            await registry.resolve("sales", tenant_id=7)
        assert provider.lookup.calls == 1

    # 方法作用：验证 Registry 对 positional-only Provider 租户参数的兼容调用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_positional_only_provider_tenant_is_supported(self) -> None:
        """Provider 的位置参数租户签名不得在解析时失败。"""
        # Arrange
        class _Provider:
            async def lookup(self, name: str, tenant_id: int | None = None, /):
                return DataSourceConfig(
                    name=name,
                    dialect="sqlite",
                    mode="embedded",
                    tenant_id=tenant_id or 1,
                )

            async def test_connection(self, _config: DataSourceConfig) -> bool:
                return True

        registry = DataSourceRegistry()
        registry.register_provider("positional", _Provider())
        registry._create_engine = lambda _config: asyncio.sleep(0, result=SimpleNamespace())  # type: ignore[method-assign]

        # Act
        resolved = await registry.resolve("sales", tenant_id=7)

        # Assert
        assert resolved.tenant_id == 7

    # 方法作用：验证失败解析不会把临时锁和代次元数据永久留在 Registry。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_transient_resolution_state_is_reclaimed(self) -> None:
        """大量无效数据源名称不能造成锁缓存随请求数量无限增长。"""
        # Arrange
        registry = DataSourceRegistry()

        # Act
        for index in range(32):
            with pytest.raises(Exception):
                await registry.resolve(f"missing_{index}", tenant_id=index + 1)
            registry.invalidate(f"missing_{index}", tenant_id=index + 1)

        # Assert
        assert len(registry._resolve_locks) == 0  # noqa: SLF001
        assert len(registry._resolution_generations) == 0  # noqa: SLF001

    # 方法作用：验证成功缓存仍可复用，但解析锁和代次只在活动期间保留。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_cached_resolution_releases_coordination_state(self) -> None:
        """正常解析后只保留业务缓存，不保留无用协调对象。"""
        # Arrange
        class _Provider:
            async def lookup(self, name: str, *, tenant_id: int | None = None):
                return DataSourceConfig(
                    name=name,
                    dialect="sqlite",
                    mode="embedded",
                    tenant_id=tenant_id or 1,
                )

            async def test_connection(self, _config: DataSourceConfig) -> bool:
                return True

        registry = DataSourceRegistry()
        registry.register_provider("test", _Provider())
        registry._create_engine = lambda _config: asyncio.sleep(0, result=SimpleNamespace())  # type: ignore[method-assign]

        # Act
        resolved = await registry.resolve("sales", tenant_id=7)

        # Assert
        assert resolved.name == "sales"
        assert (7, "sales") in registry._cache  # noqa: SLF001
        assert len(registry._resolve_locks) == 0  # noqa: SLF001
        assert len(registry._resolution_generations) == 0  # noqa: SLF001
