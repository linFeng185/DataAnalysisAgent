"""Schema 缓存刷新失败保护回归测试。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
class TestCacheRefreshSafety:
    """覆盖刷新失败时旧快照的保留语义。"""

    async def test_reload_failure_keeps_expired_cache(self) -> None:
        """数据库内省失败时不能先删除仍可用于降级的旧 Schema。"""
        # Arrange
        from src.knowledge.cache_refresher import CacheRefresher

        old = datetime.now(timezone.utc) - timedelta(days=8)
        expired = SimpleNamespace(
            id="table:sales.orders",
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
        manager = SimpleNamespace(refresh=AsyncMock(side_effect=RuntimeError("offline")))

        # Act
        deleted = await CacheRefresher(
            schema_manager=manager,
            vector_store=store,
        ).refresh_expired()

        # Assert
        assert deleted == 0
        store.delete_by_ids.assert_not_awaited()
