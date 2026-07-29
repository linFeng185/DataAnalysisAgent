"""Schema 缓存自动刷新测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema


@pytest.mark.asyncio
class TestCacheRefresher:
    """覆盖功能 6.4.1、6.4.2、6.4.3、6.4.5。"""

    # 方法作用：验证 Schema 指纹不受表顺序影响但能识别字段变化。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_fingerprint_is_stable_and_change_sensitive(self) -> None:
        """轮询比较必须避免顺序抖动并准确识别 DDL 变化。"""
        # Arrange
        from src.knowledge.cache_refresher import compute_schema_fingerprint

        first = SchemaSnapshot(tables=[
            TableSchema(name="orders", columns=[ColumnInfo(name="id", type="bigint")]),
            TableSchema(name="users", columns=[ColumnInfo(name="id", type="bigint")]),
        ])
        reordered = SchemaSnapshot(tables=list(reversed(first.tables)))
        changed = SchemaSnapshot(tables=[
            TableSchema(name="orders", columns=[
                ColumnInfo(name="id", type="bigint"),
                ColumnInfo(name="amount", type="decimal"),
            ]),
            TableSchema(name="users", columns=[ColumnInfo(name="id", type="bigint")]),
        ])

        # Act / Assert
        assert compute_schema_fingerprint(first) == compute_schema_fingerprint(reordered)
        assert compute_schema_fingerprint(first) != compute_schema_fingerprint(changed)

    # 方法作用：验证过期 AUTO 条目被清理并主动刷新受影响数据源。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_refresh_expired_deletes_and_reloads_datasource(self) -> None:
        """后台任务不应等到下一次用户请求才恢复过期 Schema。"""
        # Arrange
        from src.knowledge.cache_refresher import CacheRefresher

        old = datetime.now(timezone.utc) - timedelta(days=8)
        expired = SimpleNamespace(
            id="table:sales.orders",
            content="orders",
            metadata={
                "source": "auto_introspect",
                "created_at": old.isoformat(),
                "ttl": str(7 * 24 * 60 * 60),
                "datasource": "sales",
            },
        )
        store = SimpleNamespace(
            get_by_filter=AsyncMock(return_value=[expired]),
            delete_by_ids=AsyncMock(return_value=1),
        )
        manager = SimpleNamespace(refresh=AsyncMock())
        refresher = CacheRefresher(schema_manager=manager, vector_store=store)

        # Act
        deleted = await refresher.refresh_expired()

        # Assert
        assert deleted == 1
        store.delete_by_ids.assert_awaited_once_with(["table:sales.orders"])
        manager.refresh.assert_awaited_once_with("sales")

    # 方法作用：验证轮询只在指纹变化时使用已内省快照替换缓存。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_refresh_on_schema_change_uses_snapshot_once(self) -> None:
        """首次轮询建立基线，后续 DDL 变化触发一次刷新。"""
        # Arrange
        from src.knowledge.cache_refresher import CacheRefresher

        first = SchemaSnapshot(tables=[TableSchema(
            name="orders", columns=[ColumnInfo(name="id", type="bigint")],
        )])
        changed = SchemaSnapshot(tables=[TableSchema(
            name="orders",
            columns=[ColumnInfo(name="id", type="bigint"), ColumnInfo(name="amount", type="decimal")],
        )])
        manager = SimpleNamespace(
            inspect_live_schema=AsyncMock(side_effect=[first, changed]),
            refresh_from_snapshot=AsyncMock(),
        )
        refresher = CacheRefresher(schema_manager=manager, vector_store=SimpleNamespace())

        # Act
        baseline_changed = await refresher.refresh_on_schema_change("sales")
        ddl_changed = await refresher.refresh_on_schema_change("sales")

        # Assert
        assert baseline_changed is False
        assert ddl_changed is True
        manager.refresh_from_snapshot.assert_awaited_once_with("sales", changed)

    # 方法作用：验证未获取 Redis SET NX 锁时不会重复执行同一数据源刷新。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_redis_lock_skips_duplicate_refresh(self) -> None:
        """多实例并发轮询时只有锁持有者可以内省和写缓存。"""
        # Arrange
        from src.knowledge.cache_refresher import CacheRefresher

        redis = SimpleNamespace(set=AsyncMock(return_value=False), delete=AsyncMock())
        manager = SimpleNamespace(inspect_live_schema=AsyncMock())
        refresher = CacheRefresher(
            schema_manager=manager,
            vector_store=SimpleNamespace(),
            redis_client=redis,
        )

        # Act
        changed = await refresher.refresh_on_schema_change("sales")

        # Assert
        assert changed is False
        redis.set.assert_awaited_once()
        assert redis.set.await_args.kwargs == {"nx": True, "ex": 60}
        manager.inspect_live_schema.assert_not_awaited()

    # 方法作用：验证周期服务聚合过期清理和全部已注册数据源的指纹检查。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_service_run_once_reports_refresh_summary(self) -> None:
        """单轮任务应逐个检查数据源并返回可观测摘要。"""
        # Arrange
        from src.knowledge.cache_refresher import CacheRefreshService

        refresher = SimpleNamespace(
            refresh_expired=AsyncMock(return_value=2),
            refresh_on_schema_change=AsyncMock(side_effect=[True, False]),
            close=AsyncMock(),
        )
        registry = SimpleNamespace(list_all=AsyncMock(return_value=[
            {"name": "sales"}, {"name": "warehouse"},
        ]))
        service = CacheRefreshService(refresher, registry, interval_seconds=3600)

        # Act
        result = await service.run_once()

        # Assert
        assert result == {"deleted": 2, "checked": 2, "changed": 1}
        assert refresher.refresh_on_schema_change.await_count == 2

    # 方法作用：验证周期服务可以幂等启动并在关闭时释放刷新器。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_service_start_and_close_are_lifecycle_safe(self) -> None:
        """应用关闭不能遗留 Schema 刷新后台任务。"""
        # Arrange
        from src.knowledge.cache_refresher import CacheRefreshService

        refresher = SimpleNamespace(close=AsyncMock())
        registry = SimpleNamespace()
        service = CacheRefreshService(refresher, registry, interval_seconds=3600)

        # Act
        await service.start()
        running = service.running
        await service.start()
        await service.close()

        # Assert
        assert running is True
        assert service.running is False
        refresher.close.assert_awaited_once()
