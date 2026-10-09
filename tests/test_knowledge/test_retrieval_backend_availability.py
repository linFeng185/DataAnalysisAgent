"""真实检索层可用性回归：区分「后端整体不可用」与「确实零命中」。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.knowledge.retrieval import KnowledgeBackendUnavailable, search_knowledge


# 方法作用：构造一个所有后端接口都失败的存储替身。
# Args: error - 两个接口共同抛出的异常。
# Returns: 具备 search/get_by_filter 的存储替身。
def _broken_store(error: Exception) -> SimpleNamespace:
    return SimpleNamespace(
        search=AsyncMock(side_effect=error),
        get_by_filter=AsyncMock(side_effect=error),
    )


# 方法作用：构造一个可返回空结果但无故障的存储替身。
# Args: 无。
# Returns: 返回空命中的存储替身。
def _empty_store() -> SimpleNamespace:
    return SimpleNamespace(
        search=AsyncMock(return_value=[]),
        get_by_filter=AsyncMock(return_value=[]),
    )


class TestRetrievalBackendAvailability:
    """覆盖 27.1.2/27.1.3：严格模式下后端故障不得伪装成零命中。"""

    # 方法作用：预检索调用点默认行为不变：后端故障仍 fail-open 返回空列表。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_default_mode_stays_fail_open_for_preexisting_callers(self, monkeypatch) -> None:
        """预检索调用点默认行为不变：后端故障仍 fail-open 返回空列表。"""
        # Arrange
        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: True)

        # Act
        hits = await search_knowledge(
            _broken_store(ConnectionError("synthetic vector backend unavailable")),
            "status",
        )

        # Assert
        assert hits == []

    # 方法作用：所有范围分组都失败时必须抛 KnowledgeBackendUnavailable，不能返回空。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_strict_mode_raises_when_every_scope_fails(self, monkeypatch) -> None:
        """所有范围分组都失败时必须抛 KnowledgeBackendUnavailable，不能返回空。"""
        # Arrange
        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: True)

        # Act + Assert
        with pytest.raises(KnowledgeBackendUnavailable):
            await search_knowledge(
                _broken_store(ConnectionError("synthetic vector backend unavailable")),
                "status",
                fail_on_backend_error=True,
            )

    # 方法作用：后端健康但无命中仍是零命中，严格模式不得误报不可用。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_strict_mode_keeps_empty_result_when_backend_healthy(self, monkeypatch) -> None:
        """后端健康但无命中仍是零命中，严格模式不得误报不可用。"""
        # Arrange
        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: True)

        # Act
        hits = await search_knowledge(
            _empty_store(),
            "status",
            fail_on_backend_error=True,
        )

        # Assert
        assert hits == []

    # 方法作用：策略为 fail-closed 时，原异常直接向上传播而不是被吞掉。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_strict_mode_respects_fail_closed_policy(self, monkeypatch) -> None:
        """策略为 fail-closed 时，原异常直接向上传播而不是被吞掉。"""
        # Arrange
        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: False)

        # Act + Assert
        with pytest.raises(ConnectionError):
            await search_knowledge(
                _broken_store(ConnectionError("synthetic vector backend unavailable")),
                "status",
                fail_on_backend_error=True,
            )

    # 方法作用：严格模式的失败路径同样不得把查询正文写进日志。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_strict_mode_logs_no_query_content(self, monkeypatch) -> None:
        """严格模式的失败路径同样不得把查询正文写进日志。"""
        # Arrange
        import src.knowledge.retrieval as module

        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: True)
        logger = MagicMock()
        monkeypatch.setattr(module, "logger", logger)

        # Act
        hits = await search_knowledge(
            _broken_store(ConnectionError("synthetic vector backend unavailable")),
            "synthetic-secret-query",
        )

        # Assert
        assert hits == []
        assert "synthetic-secret-query" not in repr(logger.mock_calls)

    # 方法作用：真实命中路径（含重排）不得把查询正文写进任何日志。
    # Args: monkeypatch - pytest 内置夹具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    async def test_real_hit_path_logs_no_query_content(self, monkeypatch) -> None:
        """覆盖真实检索与重排的下层日志，而不是只 patch 掉上层工具。"""
        # Arrange
        import src.knowledge.reranker as reranker_module
        import src.knowledge.retrieval as module
        from src.memory.vector_store import VectorSearchResult

        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: True)
        retrieval_logger = MagicMock()
        reranker_logger = MagicMock()
        monkeypatch.setattr(module, "logger", retrieval_logger)
        monkeypatch.setattr(reranker_module, "logger", reranker_logger)
        store = SimpleNamespace(
            search=AsyncMock(return_value=[VectorSearchResult(
                id="doc-1",
                content="orders.status 合法取值为 paid 和 cancelled",
                metadata={"category": "enum_value", "document_version": "v1"},
                score=0.9,
            )]),
            get_by_filter=AsyncMock(return_value=[]),
        )

        # Act
        hits = await search_knowledge(store, "synthetic-secret-query")

        # Assert：真实命中并重排，但两个日志器都不出现查询正文。
        assert hits and hits[0].source_id == "doc-1"
        assert "synthetic-secret-query" not in repr(retrieval_logger.mock_calls)
        assert "synthetic-secret-query" not in repr(reranker_logger.mock_calls)
        assert not [call for call in retrieval_logger.info.call_args_list]
