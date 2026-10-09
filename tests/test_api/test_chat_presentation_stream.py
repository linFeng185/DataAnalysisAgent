"""覆盖 28.2、28.3 的实际 LangGraph 回调到 SSE 协议边界。"""

from typing import Any
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessageChunk
from langgraph.graph import END, START, StateGraph

from src.config import Settings
from src.graph.state import AnalysisState
from tests.test_api.test_streaming_tool_round import _FakeApp, _parse_sse, _stub_background


class TestChatPresentationStream:
    """不访问真实模型，验证数据/正文在最终结果前实际产出。"""

    # 验证真正的图执行和模型 astream 产生公开正文增量。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_real_graph_callbacks_stream_before_final(self, monkeypatch) -> None:
        """不能仅断言字符串拆分，必须经过实际 LangGraph 模型回调。"""
        import src.api.streaming as streaming
        import src.graph.workflow as workflow
        from src.llm.invocation import PreparedInvocation
        from src.llm.knowledge_loop import invoke_with_knowledge

        # Arrange
        model = FakeListChatModel(responses=['{"summary":"销售持续增长"}'])

        # 模拟已经安全执行且脱敏的数据边界。
        # Args: state - 当前图输入。
        # Returns: 查询结果样本。
        async def execute(state: AnalysisState) -> dict:
            return {
                "query_result_sample": [{"region": "A", "sales": 1}],
                "query_result_full_count": 1,
            }

        # 通过统一模型循环执行实际 Fake LLM 流式调用。
        # Args: state - 图状态。
        # Returns: 完整分析结果。
        async def analyze(state: AnalysisState) -> dict:
            prepared = PreparedInvocation("analysis.result", "1", "analyze_result", model, [], {})
            response = await invoke_with_knowledge(prepared)
            assert response.content == '{"summary":"销售持续增长"}'
            return {"analysis_result": {"summary": "销售持续增长"}}

        # 汇总已验证结果，不进行外部历史写入。
        # Args: state - 图状态。
        # Returns: 最终公开响应。
        async def finish(state: AnalysisState) -> dict:
            return {
                "final_response": {
                    "success": True,
                    "data": state["query_result_sample"],
                    "analysis": state["analysis_result"],
                }
            }

        graph = StateGraph(AnalysisState)
        graph.add_node("execute_sql", execute)
        graph.add_node("analyze_result", analyze)
        graph.add_node("build_response", finish)
        graph.add_edge(START, "execute_sql")
        graph.add_edge("execute_sql", "analyze_result")
        graph.add_edge("analyze_result", "build_response")
        graph.add_edge("build_response", END)
        monkeypatch.setattr(workflow, "app", graph.compile())
        monkeypatch.setattr(
            "src.llm.knowledge_loop.build_knowledge_tools_for_call", lambda prepared=None: []
        )
        _stub_background(monkeypatch)
        # Act
        chunks = [
            chunk
            async for chunk in streaming.stream_analysis(
                "销售趋势",
                "",
                "stream-real-graph",
                tenant_id=2,
                user_id=7,
                user_role="analyst",
                datasource_access={},
            )
        ]
        parsed = _parse_sse(chunks)
        kinds = [name for name, _ in parsed]
        deltas = [event["content"] for name, event in parsed if name == "answer_delta"]
        # Assert
        assert len(deltas) > 1
        assert "".join(deltas) == "销售持续增长"
        assert kinds.index("data") < kinds.index("answer_delta") < kinds.index("result")
        assert kinds[-1] == "done"

    # 验证全局过程开关及原始推理抑制。
    # Args: self - 测试实例；monkeypatch - 补丁工具；enabled - 平台配置。
    # Returns: 无返回值。
    @pytest.mark.parametrize("enabled", [False, True])
    async def test_process_gate_and_raw_reasoning(self, monkeypatch, enabled: bool) -> None:
        """管理员开启后也只返回受控摘要，不返回 reasoning_content。"""
        import src.api.streaming as streaming
        import src.graph.workflow as workflow

        # Arrange
        events = [
            {"event": "on_chain_start", "name": "analyze_result", "run_id": "n1", "data": {}},
            {
                "event": "on_chat_model_start",
                "name": "model",
                "run_id": "m1",
                "metadata": {"langgraph_node": "analyze_result"},
                "data": {},
            },
            {
                "event": "on_chat_model_stream",
                "run_id": "m1",
                "metadata": {"langgraph_node": "analyze_result"},
                "data": {
                    "chunk": AIMessageChunk(
                        content='{"summary":"公开正文"}',
                        additional_kwargs={"reasoning_content": "私密内部推理"},
                    )
                },
            },
            {
                "event": "on_chain_end",
                "name": "analyze_result",
                "run_id": "n1",
                "data": {"output": {"analysis_result": {"summary": "公开正文"}}},
            },
        ]
        monkeypatch.setattr(workflow, "app", _FakeApp(events))
        monkeypatch.setattr(
            streaming, "get_settings", lambda: Settings(chat_show_analysis_process=enabled)
        )
        _stub_background(monkeypatch)
        # Act
        chunks = [
            chunk
            async for chunk in streaming.stream_analysis(
                "销售趋势",
                "",
                "stream-process",
                tenant_id=2,
                user_id=7,
                user_role="analyst",
                datasource_access={},
            )
        ]
        parsed = _parse_sse(chunks)
        # Assert
        assert bool([event for name, event in parsed if name == "process_summary"]) is enabled
        assert "私密内部推理" not in "".join(chunks)
        assert all(name != "thinking" for name, _ in parsed)

    # 验证异常后结束为失败、保留一条边界异常日志并允许新请求恢复。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_stream_failure_then_recovery(self, monkeypatch) -> None:
        """一次故障不能让下一轮继承公开流作用域或错误状态。"""
        import src.api.streaming as streaming
        import src.graph.workflow as workflow

        # Arrange
        class FailedGraph:
            """在异步事件入口抛错的替身。"""

            # 制造可控错误，不泄露实际服务异常。
            # Args: args/kwargs - 图调用参数。
            # Returns: 不会成功产出事件。
            async def astream_events(self, *args: Any, **kwargs: Any):
                raise RuntimeError("internal-secret")
                yield {}

        monkeypatch.setattr(workflow, "app", FailedGraph())
        _stub_background(monkeypatch)
        error_logger = Mock()
        monkeypatch.setattr(streaming.logger, "error", error_logger)
        # Act
        chunks = [
            chunk
            async for chunk in streaming.stream_analysis(
                "分析",
                "",
                "failure",
                tenant_id=2,
                user_id=7,
                user_role="analyst",
                datasource_access={},
            )
        ]
        monkeypatch.setattr(workflow, "app", _FakeApp([]))
        recovered = [
            chunk
            async for chunk in streaming.stream_analysis(
                "重试",
                "",
                "recovery",
                tenant_id=2,
                user_id=7,
                user_role="analyst",
                datasource_access={},
            )
        ]
        # Assert
        assert _parse_sse(chunks)[-1][1]["status"] == "error"
        assert "internal-secret" not in "".join(chunks)
        assert _parse_sse(recovered)[-1][1]["status"] == "complete"
        error_logger.assert_called_once()
        assert error_logger.call_args.kwargs["exc_info"] is True
