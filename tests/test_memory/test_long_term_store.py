"""长期记忆回退路径日志回归测试。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


logger = logging.getLogger(__name__)


class TestLongTermStoreFallback:
    """覆盖偏好查询双存储故障的可见回退。"""

    # 方法作用：验证 ChromaDB 偏好查询异常返回空字典时记录堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chroma_preference_failure_logs_exception(self, monkeypatch) -> None:
        """向量存储故障不得伪装成用户没有偏好。"""
        logger.debug("test_chroma_preference_failure_logs_exception 入口")
        try:
            # Arrange
            import src.memory.long_term_store as store_module
            import src.memory.vector_store as vector_module

            captured_logger = MagicMock()
            monkeypatch.setattr(store_module, "logger", captured_logger)
            monkeypatch.setattr(
                vector_module,
                "get_vector_store",
                AsyncMock(side_effect=RuntimeError("vector unavailable")),
            )
            store = store_module.LongTermMemoryStore()

            # Act
            result = await store._get_prefs_from_chroma("user-1")  # noqa: SLF001

            # Assert
            assert result == {}
            captured_logger.error.assert_called_once()
            assert captured_logger.error.call_args.kwargs["exc_info"] is True
            logger.info("test_chroma_preference_failure_logs_exception 完成")
        except Exception as exc:
            logger.error(
                "test_chroma_preference_failure_logs_exception 异常: %s",
                exc,
                exc_info=True,
            )
            raise


class TestLongTermMemoryIsolation:
    """覆盖长期记忆的身份隔离、过期过滤和双后端一致性。"""

    # 方法作用：构造带指定 metadata 的向量检索结果。
    # Args: entry_id - 结果 ID；visibility/tenant_id/owner_user_id - 作用域；created_at/ttl_days/confidence - 生命周期。
    # Returns: VectorSearchResult 测试对象。
    @staticmethod
    def _result(
        entry_id: str,
        *,
        visibility: str,
        tenant_id: int,
        owner_user_id: int,
        created_at: datetime | None = None,
        ttl_days: int | None = None,
        confidence: float = 0.9,
    ):
        from src.memory.vector_store import VectorSearchResult

        return VectorSearchResult(
            id=entry_id,
            content=f"memory-{entry_id}",
            score=0.9,
            metadata={
                "resource_kind": "long_term_memory",
                "memory_type": "correction",
                "visibility": visibility,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "scope": f"{visibility}:{tenant_id}:{owner_user_id}",
                "payload_json": "{}",
                "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
                "last_accessed_at": datetime.now(timezone.utc).isoformat(),
                "access_count": 0,
                "confidence": confidence,
                "ttl_days": ttl_days if ttl_days is not None else -1,
            },
        )

    # 方法作用：验证长期记忆按 system、tenant、private 三次精确检索并过滤过期结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_filters_visibility_expiry_and_confidence(self) -> None:
        """只返回当前身份可见、未过期且达到最低置信度的记忆。"""
        # Arrange
        from src.memory.long_term_store import LongTermMemoryStore
        from src.security.tenant_policy import RequestIdentity, TenantPolicy

        expired = self._result(
            "expired",
            visibility="private",
            tenant_id=4,
            owner_user_id=7,
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
            ttl_days=1,
        )
        low = self._result(
            "low",
            visibility="tenant",
            tenant_id=4,
            owner_user_id=0,
            confidence=0.2,
        )
        visible = self._result(
            "visible",
            visibility="private",
            tenant_id=4,
            owner_user_id=7,
        )
        vector = SimpleNamespace(
            search=AsyncMock(side_effect=[[low], [], [expired, visible]]),
            upsert=AsyncMock(return_value=1),
            delete_by_ids=AsyncMock(return_value=1),
        )
        pg = SimpleNamespace(execute=AsyncMock(return_value="UPDATE 1"))
        store = LongTermMemoryStore(pg_pool=pg, vector_store=vector, policy=TenantPolicy(True))
        identity = RequestIdentity(tenant_id=4, user_id=7, role="analyst")

        # Act
        result = await store.search("退款", identity=identity, top_k=5)

        # Assert
        assert [item.id for item in result] == ["visible"]
        filters = [call.kwargs["filters"] for call in vector.search.await_args_list]
        assert filters == [
            {"resource_kind": "long_term_memory", "visibility": "system"},
            {"resource_kind": "long_term_memory", "visibility": "tenant", "tenant_id": 4},
            {
                "resource_kind": "long_term_memory",
                "visibility": "private",
                "tenant_id": 4,
                "owner_user_id": 7,
            },
        ]
        vector.delete_by_ids.assert_awaited_once_with(["expired"])
        assert pg.execute.await_count >= 2
        vector.upsert.assert_awaited_once()

    # 方法作用：验证向量后端返回越界结果时业务层仍执行租户所有者复核。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_rejects_foreign_private_memory_defensively(self) -> None:
        """后端过滤异常不能导致其他租户或用户的私有记忆进入 Prompt。"""
        # Arrange
        from src.memory.long_term_store import LongTermMemoryStore
        from src.security.tenant_policy import RequestIdentity, TenantPolicy

        foreign = self._result(
            "foreign",
            visibility="private",
            tenant_id=8,
            owner_user_id=9,
        )
        vector = SimpleNamespace(
            search=AsyncMock(side_effect=[[foreign], [foreign], [foreign]]),
            upsert=AsyncMock(),
            delete_by_ids=AsyncMock(),
        )
        store = LongTermMemoryStore(vector_store=vector, policy=TenantPolicy(True))

        # Act
        result = await store.search(
            "query",
            identity=RequestIdentity(tenant_id=4, user_id=7, role="analyst"),
        )

        # Assert
        assert result == []
        vector.upsert.assert_not_awaited()
        vector.delete_by_ids.assert_not_awaited()

    # 方法作用：验证成功 SQL 模板写入当前租户用户的私有作用域。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_save_sql_template_persists_identity_metadata(self) -> None:
        """自动学习的未审核 SQL 不得默认跨用户或跨租户共享。"""
        # Arrange
        from src.memory.long_term_store import LongTermMemoryStore
        from src.security.tenant_policy import RequestIdentity, TenantPolicy

        vector = SimpleNamespace(
            delete_by_ids=AsyncMock(return_value=0),
            upsert=AsyncMock(return_value=1),
        )
        pg = SimpleNamespace(execute=AsyncMock(return_value="INSERT 0 1"))
        store = LongTermMemoryStore(pg_pool=pg, vector_store=vector, policy=TenantPolicy(True))
        identity = RequestIdentity(tenant_id=4, user_id=7, role="analyst")

        # Act
        memory = await store.save_sql_template(
            "订单数",
            "SELECT COUNT(*) FROM orders",
            "postgres",
            identity=identity,
        )

        # Assert
        assert memory.visibility == "private"
        assert memory.tenant_id == 4
        assert memory.owner_user_id == 7
        metadata = vector.upsert.await_args.args[0][0].metadata
        assert metadata["tenant_id"] == 4
        assert metadata["owner_user_id"] == 7
        assert metadata["visibility"] == "private"
        assert metadata["resource_kind"] == "long_term_memory"

    # 方法作用：验证过期和低置信度清理同步删除 PostgreSQL 与 VectorStore。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prune_deletes_from_postgres_and_vector_store(self) -> None:
        """任何一侧残留都会导致已删除记忆再次被召回。"""
        # Arrange
        from src.memory.long_term_store import LongTermMemoryStore

        pg = SimpleNamespace(
            fetch=AsyncMock(side_effect=[[{"id": "expired"}], [{"id": "low"}]]),
        )
        vector = SimpleNamespace(delete_by_ids=AsyncMock(return_value=1))
        store = LongTermMemoryStore(pg_pool=pg, vector_store=vector)

        # Act
        expired_count = await store.prune_expired()
        low_count = await store.prune_low_confidence()

        # Assert
        assert expired_count == 1
        assert low_count == 1
        assert vector.delete_by_ids.await_args_list[0].args[0] == ["expired"]
        assert vector.delete_by_ids.await_args_list[1].args[0] == ["low"]

    # 方法作用：验证 PG 补偿队列能把已提交记忆重新同步到 VectorStore。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reconcile_pending_vector_sync(self) -> None:
        """PG 成功而向量写入失败后，维护任务必须最终恢复双后端一致。"""
        # Arrange
        from src.memory.long_term_store import LongTermMemoryStore

        now = datetime.now(timezone.utc)
        pg = SimpleNamespace(
            fetch=AsyncMock(return_value=[{
                "id": "pending-1",
                "memory_type": "sql_template",
                "scope": "private:4:7",
                "visibility": "private",
                "tenant_id": 4,
                "owner_user_id": 7,
                "content": "问题: 订单数",
                "payload": {"question": "订单数", "sql": "SELECT 1", "dialect": "postgres"},
                "created_at": now,
                "last_accessed_at": now,
                "access_count": 0,
                "confidence": 0.5,
                "ttl_days": 180,
            }]),
            execute=AsyncMock(return_value="DELETE 1"),
        )
        vector = SimpleNamespace(
            delete_by_ids=AsyncMock(return_value=0),
            upsert=AsyncMock(return_value=1),
        )
        store = LongTermMemoryStore(pg_pool=pg, vector_store=vector)

        # Act
        count = await store.reconcile_pending_sync()

        # Assert
        assert count == 1
        vector.upsert.assert_awaited_once()
        assert "DELETE FROM pending_vector_sync" in pg.execute.await_args.args[0]
