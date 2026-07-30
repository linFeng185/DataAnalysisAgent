"""流式端点测试 — SSE 格式 / StreamingResponse 配置。"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

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

        from src.api.auth import create_access_token
        from src.main import app
        token = create_access_token(9, 4, "analyst")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            response = await client.post("/api/v1/chat/stream", json={})
        assert response.status_code == 422

    async def test_stream_endpoint_post_method(self):
        from httpx import ASGITransport, AsyncClient

        from src.api.auth import create_access_token
        from src.main import app
        token = create_access_token(9, 4, "analyst")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
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


class TestStreamTerminalState:
    """覆盖 SSE 异常终态和非有限数值序列化。"""

    # 方法作用：验证工作流异常后终态明确标记失败，不能再宣告成功完成。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_stream_error_is_not_followed_by_complete_done(self, monkeypatch) -> None:
        """`error` 后只能发送失败终态，不能发送 `status=complete`。"""
        logger.debug("test_stream_error_is_not_followed_by_complete_done 入口")
        # Arrange
        import src.api.background_tasks as background_tasks
        import src.graph.workflow as workflow
        from src.api.streaming import stream_analysis

        class FailingWorkflow:
            """在首个进度事件后抛出固定异常。"""

            # 方法作用：模拟 LangGraph 在流式处理中失败。
            # Args: self - 模拟工作流；args/kwargs - 被测方法透传参数。
            # Returns: 先产生一个事件，再抛出 RuntimeError。
            async def astream_events(self, *args, **kwargs):
                logger.debug("FailingWorkflow.astream_events 入口")
                del args, kwargs
                yield {"event": "on_chain_start", "name": "generate_sql"}
                logger.error("FailingWorkflow.astream_events 异常", exc_info=True)
                raise RuntimeError("stream failed")

        # 方法作用：关闭测试中创建的后台协程，避免访问真实 SessionStore。
        # Args: coroutine - 待丢弃协程；args/kwargs - 后台任务元数据。
        # Returns: None。
        def discard_background_task(coroutine, *args, **kwargs) -> None:
            logger.debug("discard_background_task 入口")
            del args, kwargs
            coroutine.close()
            logger.info("discard_background_task 完成")

        monkeypatch.setattr(workflow, "app", FailingWorkflow())
        monkeypatch.setattr(background_tasks, "create_background_task", discard_background_task)

        # Act
        events = [
            json.loads(chunk.removeprefix("data: ").strip())
            async for chunk in stream_analysis(
                "查询订单",
                "demo",
                session_id="session-1",
                datasource_access={"demo": {"allowed_columns": [], "row_filter_sql": ""}},
            )
        ]

        # Assert
        assert events[-2] == {"type": "error", "message": "流式处理失败，请稍后重试"}
        assert events[-1] == {"type": "done", "status": "error"}
        assert not any(event.get("status") == "complete" for event in events)
        logger.info("test_stream_error_is_not_followed_by_complete_done 完成")

    # 方法作用：验证 SSE 将 NaN 和 Infinity 规范化为 JSON null。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sse_serializes_non_finite_numbers_as_null(self) -> None:
        """数据库非有限数值不得生成非法 JSON 或触发 Decimal 序列化异常。"""
        logger.debug("test_sse_serializes_non_finite_numbers_as_null 入口")
        # Arrange
        from src.api.streaming import _sse

        # Act
        payload = json.loads(_sse("result", {
            "decimal_nan": Decimal("NaN"),
            "decimal_inf": Decimal("Infinity"),
            "float_nan": float("nan"),
        }).removeprefix("data: ").strip())

        # Assert
        assert payload == {
            "type": "result",
            "decimal_nan": None,
            "decimal_inf": None,
            "float_nan": None,
        }
        logger.info("test_sse_serializes_non_finite_numbers_as_null 完成")
