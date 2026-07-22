"""流式端点测试 — SSE 格式 / StreamingResponse 配置。"""

from __future__ import annotations

import json
import logging

import pytest


logger = logging.getLogger(__name__)


class TestSSEFormat:
    def test_basic_event(self):
        from src.api.streaming import _sse
        result = _sse("test", {"msg": "hello"})
        assert result.startswith("data: ")
        parsed = json.loads(result[6:].strip())
        assert parsed["type"] == "test"
        assert parsed["msg"] == "hello"

    def test_event_with_chinese(self):
        from src.api.streaming import _sse
        result = _sse("progress", {"node": "generate_sql", "message": "正在生成 SQL..."})
        parsed = json.loads(result[6:].strip())
        assert parsed["type"] == "progress"
        assert "SQL" in parsed["message"]

    def test_all_known_nodes_have_progress(self):
        from src.api.streaming import _PROGRESS_MAP
        expected_nodes = [
            "classify_intent", "retrieve_schema", "generate_sql",
            "layer3_validate", "layer4_explain", "execute_sql",
            "analyze_result", "generate_chart", "build_response",
        ]
        for node in expected_nodes:
            assert node in _PROGRESS_MAP, f"缺少 {node} 的进度描述"


class TestStreamingResponseConfig:
    async def test_stream_endpoint_mounted(self):
        from httpx import ASGITransport, AsyncClient
        from src.main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post("/api/v1/chat/stream", json={})
        assert response.status_code == 422

    async def test_stream_endpoint_post_method(self):
        from httpx import ASGITransport, AsyncClient
        from src.main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/chat/stream")
        assert response.status_code == 405


class TestFindParentNode:
    def test_from_metadata_langgraph_node(self):
        from src.api.streaming import _find_parent_node
        event = {"metadata": {"langgraph_node": "generate_sql"}}
        assert _find_parent_node(event) == "generate_sql"

    def test_skip_runnable(self):
        from src.api.streaming import _find_parent_node
        event = {"metadata": {"langgraph_node": "RunnableSequence"}}
        assert _find_parent_node(event) is None

    def test_from_tags(self):
        from src.api.streaming import _find_parent_node
        event = {"metadata": {}, "tags": ["LangGraph", "generate_sql"]}
        assert _find_parent_node(event) == "generate_sql"

    def test_no_match(self):
        from src.api.streaming import _find_parent_node
        event = {"metadata": {}, "tags": []}
        assert _find_parent_node(event) is None


class TestStreamIdentity:
    """覆盖并行 LLM SSE 调用实例隔离。"""

    # 方法作用：验证同名节点的不同模型调用使用各自 run_id 作为流标识。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_event_stream_id_uses_run_id(self):
        """两个 generate_sql 调用不得因为节点名相同而共享缓冲区。"""
        logger.debug("test_event_stream_id_uses_run_id 入口")
        try:
            # Arrange
            from src.api.streaming import _event_stream_id

            first = {"run_id": "run-mysql", "metadata": {"langgraph_node": "generate_sql"}}
            second = {"run_id": "run-postgres", "metadata": {"langgraph_node": "generate_sql"}}

            # Act
            first_id = _event_stream_id(first)
            second_id = _event_stream_id(second)

            # Assert
            assert first_id == "run-mysql"
            assert second_id == "run-postgres"
            assert first_id != second_id
            logger.info("test_event_stream_id_uses_run_id 完成")
        except Exception as exc:
            logger.error("test_event_stream_id_uses_run_id 异常: %s", exc, exc_info=True)
            raise
