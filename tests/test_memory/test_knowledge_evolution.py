"""知识库自进化测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestKnowledgeEvolution:
    """覆盖知识库自进化功能 19.3。"""

    # 方法作用：验证未执行或未验证的 SQL 不会进入自进化观察表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_unverified_sql_is_ignored(self) -> None:
        """失败 SQL 必须被忽略并返回原因。"""
        # Arrange
        from src.memory.knowledge_evolution import KnowledgeEvolutionService
        from src.security.tenant_policy import RequestIdentity

        service = KnowledgeEvolutionService(memory_store=SimpleNamespace(), promotion_threshold=2)

        # Act
        result = await service.observe_successful_sql(
            "查询订单",
            "SELECT 1",
            "postgres",
            execution_success=False,
            validated=True,
            identity=RequestIdentity(tenant_id=4, user_id=7, role="analyst"),
        )

        # Assert
        assert result.promoted is False
        assert result.ignored_reason == "execution_failed"

    # 方法作用：验证同一 SQL 达到阈值后晋升为 LEARNED_PATTERN。
    # Args: self - pytest 测试类实例；identity - 当前身份。
    # Returns: 无返回值。
    async def test_repeated_verified_sql_is_promoted(self) -> None:
        """稳定指纹重复观察达到阈值后只写入私有学习模式。"""
        # Arrange
        from src.memory.knowledge_evolution import KnowledgeEvolutionService
        from src.security.tenant_policy import RequestIdentity

        identity = RequestIdentity(tenant_id=4, user_id=7, role="analyst")
        memory_store = SimpleNamespace(save_learned_pattern=AsyncMock())
        service = KnowledgeEvolutionService(memory_store=memory_store, promotion_threshold=2)

        # Act
        first = await service.observe_successful_sql(
            "订单数", "SELECT COUNT(*) FROM orders", "postgres",
            execution_success=True, validated=True, identity=identity,
        )
        second = await service.observe_successful_sql(
            "订单数", " SELECT  COUNT(*)  FROM orders ", "postgres",
            execution_success=True, validated=True, identity=identity,
        )

        # Assert
        assert first.promoted is False
        assert second.promoted is True
        memory_store.save_learned_pattern.assert_awaited_once()
        kwargs = memory_store.save_learned_pattern.await_args.kwargs
        assert kwargs["identity"] == identity
        assert kwargs["visibility"] == "private"
        assert kwargs["observation_count"] == 2

    # 方法作用：验证不同租户使用相同 SQL 指纹时观察计数相互隔离。
    # Args: self - pytest 测试类实例；无外部依赖。
    # Returns: 无返回值。
    async def test_observations_are_tenant_isolated(self) -> None:
        """同一 SQL 在不同租户分别计数，不能跨租户晋升。"""
        # Arrange
        from src.memory.knowledge_evolution import KnowledgeEvolutionService
        from src.security.tenant_policy import RequestIdentity

        memory_store = SimpleNamespace(save_learned_pattern=AsyncMock())
        service = KnowledgeEvolutionService(memory_store=memory_store, promotion_threshold=2)

        # Act
        await service.observe_successful_sql(
            "q", "SELECT 1", "postgres", execution_success=True, validated=True,
            identity=RequestIdentity(tenant_id=1, user_id=1, role="analyst"),
        )
        result = await service.observe_successful_sql(
            "q", "SELECT 1", "postgres", execution_success=True, validated=True,
            identity=RequestIdentity(tenant_id=2, user_id=1, role="analyst"),
        )

        # Assert
        assert result.observation_count == 1
        memory_store.save_learned_pattern.assert_not_awaited()

    # 方法作用：验证长期记忆 Store 将晋升模式以私有稳定 ID 写入向量后端。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_long_term_store_saves_learned_pattern(self) -> None:
        """学习模式不得默认写入租户共享范围。"""
        # Arrange
        from src.memory.long_term_store import LongTermMemoryStore
        from src.security.tenant_policy import RequestIdentity, TenantPolicy

        vector = SimpleNamespace(delete_by_ids=AsyncMock(), upsert=AsyncMock())
        store = LongTermMemoryStore(vector_store=vector, policy=TenantPolicy(True))
        identity = RequestIdentity(tenant_id=4, user_id=7, role="analyst")

        # Act
        memory = await store.save_learned_pattern(
            "订单数",
            "SELECT COUNT(*) FROM orders",
            "postgres",
            "fingerprint-1",
            3,
            identity=identity,
        )

        # Assert
        assert memory.memory_type.value == "learned_pattern"
        assert memory.visibility == "private"
        assert memory.id.startswith("learned-pattern:")
        metadata = vector.upsert.await_args.args[0][0].metadata
        assert metadata["memory_type"] == "learned_pattern"
        assert metadata["owner_user_id"] == 7
