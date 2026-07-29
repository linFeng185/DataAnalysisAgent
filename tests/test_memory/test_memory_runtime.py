"""记忆系统主链接入与迁移测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestMemoryRuntime:
    """覆盖长期记忆读取、SQL 学习和生产表迁移。"""

    # 方法作用：验证轮次准备阶段加载当前身份的偏好和相关长期记忆。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prepare_turn_loads_scoped_long_term_memory(self, monkeypatch) -> None:
        """所有意图路径都从 prepare_turn 获得同一份身份隔离记忆。"""
        # Arrange
        import src.memory.long_term_store as store_module
        from src.graph.nodes.prepare_turn import prepare_turn_node

        memory = SimpleNamespace(content="退款必须扣除手续费")
        store = SimpleNamespace(
            get_preferences=AsyncMock(return_value={"chart_type": "line"}),
            search=AsyncMock(return_value=[memory]),
        )
        monkeypatch.setattr(
            store_module,
            "get_long_term_memory_store",
            AsyncMock(return_value=store),
            raising=False,
        )

        # Act
        result = await prepare_turn_node({
            "user_query": "分析退款趋势",
            "tenant_id": 4,
            "user_id": 7,
            "user_role": "analyst",
        })

        # Assert
        assert result["user_preferences"] == {"chart_type": "line"}
        assert "退款必须扣除手续费" in result["long_term_memories_text"]
        identity = store.search.await_args.kwargs["identity"]
        assert (identity.tenant_id, identity.user_id) == (4, 7)

    # 方法作用：验证成功 SQL 响应会异步写入当前用户私有长期记忆。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_build_response_learns_successful_sql(self, monkeypatch) -> None:
        """失败、安全拦截和无 SQL 响应不能进入长期模板库。"""
        # Arrange
        import src.memory.history_store as history_module
        import src.memory.long_term_store as store_module
        from src.graph.nodes.build_response import build_response_node

        memory_store = SimpleNamespace(save_sql_template=AsyncMock())
        monkeypatch.setattr(
            store_module,
            "get_long_term_memory_store",
            AsyncMock(return_value=memory_store),
            raising=False,
        )
        monkeypatch.setattr(
            history_module,
            "get_history_store",
            lambda: SimpleNamespace(add=AsyncMock()),
        )

        # Act
        await build_response_node({
            "user_query": "订单数",
            "generated_sql": "SELECT COUNT(*) FROM orders",
            "query_result_sample": [{"count": 2}],
            "analysis_result": {"summary": "共 2 单"},
            "tenant_id": 4,
            "user_id": 7,
            "user_role": "analyst",
        })

        # Assert
        memory_store.save_sql_template.assert_awaited_once()
        identity = memory_store.save_sql_template.await_args.kwargs["identity"]
        assert (identity.tenant_id, identity.user_id) == (4, 7)

    # 方法作用：验证生产迁移创建身份隔离长期记忆、归档和补偿表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_memory_migration_defines_identity_and_rls(self) -> None:
        """长期记忆数据库层必须具备租户、所有者和三级可见性约束。"""
        # Arrange
        from pathlib import Path

        sql = Path("migrations/008_memory_runtime.sql").read_text(encoding="utf-8")

        # Act / Assert
        assert "CREATE TABLE IF NOT EXISTS long_term_memories" in sql
        assert "CREATE TABLE IF NOT EXISTS pending_vector_sync" in sql
        assert "CREATE TABLE IF NOT EXISTS sessions_archive" in sql
        assert "visibility IN ('system', 'tenant', 'private')" in sql
        assert "owner_user_id" in sql
        assert "long_term_memories_read_scope" in sql

    # 方法作用：验证达到 50 轮时 prepare_turn 自动压缩旧轮次并保留近期历史。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prepare_turn_compacts_history_at_turn_limit(self, monkeypatch) -> None:
        """短期记忆必须有确定上限，摘要后仍保留最近 30 轮。"""
        # Arrange
        import src.memory.session_archive as archive_module
        from src.graph.nodes.prepare_turn import prepare_turn_node

        compacted = [{"turn_id": 0, "analysis_summary": "早期摘要"}] + [
            {"turn_id": index, "user_query": f"问题{index}"}
            for index in range(21, 51)
        ]
        compact = AsyncMock(return_value=compacted)
        monkeypatch.setattr(archive_module, "compact_turn_history", compact, raising=False)
        history = [
            {"turn_id": index, "user_query": f"问题{index}"}
            for index in range(1, 51)
        ]

        # Act
        result = await prepare_turn_node({"user_query": "新问题", "conversation_history": history})

        # Assert
        compact.assert_awaited_once_with(history)
        assert result["conversation_history"] == compacted
