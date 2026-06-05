"""流式端点测试 — SSE 格式 / StreamingResponse 配置。"""

from __future__ import annotations

import json

import pytest


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
    def test_stream_endpoint_mounted(self):
        from src.api.routes import router
        stream_routes = [r for r in router.routes if hasattr(r, "path") and "/chat/stream" in r.path]
        assert len(stream_routes) > 0

    def test_stream_endpoint_post_method(self):
        from src.api.routes import router
        for r in router.routes:
            if hasattr(r, "path") and "/chat/stream" in r.path:
                assert "POST" in r.methods
                break


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
