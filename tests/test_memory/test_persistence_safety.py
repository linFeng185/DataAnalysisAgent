"""生产 Checkpointer 与长期记忆补偿回归测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
class TestPersistenceSafety:
    """覆盖生产持久化失败关闭和双写回滚。"""

    async def test_production_rejects_memory_checkpointer(self) -> None:
        """生产环境缺少 PostgreSQL 时必须启动失败，不能静默使用内存。"""
        # Arrange
        from src.memory.checkpointer import _create_checkpointer_resource

        settings = SimpleNamespace(env="prod", database_url="")

        # Act / Assert
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            await _create_checkpointer_resource(settings)

    async def test_pg_write_failure_rolls_back_vector_entry(self) -> None:
        """PostgreSQL 写失败而向量写成功时必须清理向量孤儿。"""
        # Arrange
        from src.memory.long_term_store import LongTermMemoryStore
        from src.security.tenant_policy import RequestIdentity, TenantPolicy

        pg = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("pg offline")))
        vector = SimpleNamespace(
            delete_by_ids=AsyncMock(return_value=1),
            upsert=AsyncMock(return_value=1),
        )
        store = LongTermMemoryStore(pg_pool=pg, vector_store=vector, policy=TenantPolicy(True))

        # Act / Assert
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            await store.save_correction(
                7,
                "错误 SQL",
                "正确 SQL",
                "用户纠正",
                identity=RequestIdentity(tenant_id=4, user_id=7, role="analyst"),
            )
        assert vector.delete_by_ids.await_count == 2
