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

    # 方法作用：验证单进程回退路径的晋升声明具备并发互斥性。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_local_promotion_claim_is_exclusive(self) -> None:
        """两个并发观察者只能有一个取得同一指纹的晋升声明。"""
        # Arrange
        import asyncio

        from src.memory.knowledge_evolution import KnowledgeEvolutionService
        from src.security.tenant_policy import RequestIdentity

        service = KnowledgeEvolutionService(memory_store=SimpleNamespace(), promotion_threshold=2)
        identity = RequestIdentity(tenant_id=4, user_id=7, role="analyst")
        key = (4, 7, "fingerprint")

        # Act
        claimed = await asyncio.gather(
            service._claim_promotion(key, identity),
            service._claim_promotion(key, identity),
        )

        # Assert
        assert sum(bool(token) for token in claimed) == 1
        assert len({token for token in claimed if token}) == 1

    # 方法作用：验证失效 Worker 的旧令牌不能覆盖新 Worker 的晋升结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_stale_promotion_claim_cannot_complete(self) -> None:
        """声明被接管后，旧 Worker 必须在写回阶段被 fencing 阻断。"""
        # Arrange
        import pytest

        from src.memory.knowledge_evolution import KnowledgeEvolutionService
        from src.security.tenant_policy import RequestIdentity

        service = KnowledgeEvolutionService(memory_store=SimpleNamespace(), promotion_threshold=2)
        identity = RequestIdentity(tenant_id=4, user_id=7, role="analyst")
        key = (4, 7, "fingerprint")
        stale_token = await service._claim_promotion(key, identity)
        service._local_claims[key] = "replacement-token"

        # Act / Assert
        with pytest.raises(RuntimeError, match="声明已失效"):
            await service._complete_promotion(key, identity, "memory-stale", stale_token)

        assert service._local_claims[key] == "replacement-token"
        assert service._local_observations.get(key, {}).get("memory_id") is None

    # 方法作用：验证晋升存储失败后会释放声明，后续观察可以重试。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_promotion_failure_releases_claim_for_retry(self) -> None:
        """一次 VectorStore 故障不能永久阻塞同一 SQL 模式。"""
        # Arrange
        from src.memory.knowledge_evolution import KnowledgeEvolutionService
        from src.security.tenant_policy import RequestIdentity

        memory_store = SimpleNamespace(
            save_learned_pattern=AsyncMock(side_effect=[RuntimeError("temporary"), SimpleNamespace(id="m-1")]),
        )
        service = KnowledgeEvolutionService(memory_store=memory_store, promotion_threshold=2)
        identity = RequestIdentity(tenant_id=4, user_id=7, role="analyst")

        # Act
        await service.observe_successful_sql(
            "订单数", "SELECT COUNT(*) FROM orders", "postgres",
            execution_success=True, validated=True, identity=identity,
        )
        failed = await service.observe_successful_sql(
            "订单数", "SELECT COUNT(*) FROM orders", "postgres",
            execution_success=True, validated=True, identity=identity,
        )
        retried = await service.observe_successful_sql(
            "订单数", "SELECT COUNT(*) FROM orders", "postgres",
            execution_success=True, validated=True, identity=identity,
        )

        # Assert
        assert failed.promoted is False
        assert retried.promoted is True
        assert retried.memory_id == "m-1"
        assert memory_store.save_learned_pattern.await_count == 2
