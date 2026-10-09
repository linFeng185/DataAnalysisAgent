"""功能27.1.2/27.1.4独立审核：真实SSE入口必须绑定已授权的数据源范围。"""

from __future__ import annotations

from tests.test_api.test_streaming_tool_round import _collect, _stub_background


class TestKnowledgeScopeReview:
    """验证作用域助手不是仅供测试使用的未接线实现。"""

    # 验证图执行时能读取到请求授权的数据源范围。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言图执行和清理阶段作用域正确。
    async def test_stream_entry_binds_request_datasource_scope(self, monkeypatch) -> None:
        """只实现use_request_knowledge_scope而不接到真实入口并不能提供请求白名单。"""
        import src.graph.workflow as workflow
        import src.llm.knowledge_loop as loop

        # Arrange：图内部记录上下文，但不发起任何真实模型或数据库调用。
        captured: dict = {}
        original = loop.get_knowledge_loop_config()

        class CapturingApp:
            """在图执行边界记录实际知识检索上下文。"""

            # 记录图执行期间的作用域并正常结束事件流。
            # Args: input_state - 图状态；config - 图配置；version - 协议版本。
            # Returns: 图结束事件异步序列。
            async def astream_events(self, input_state, config, version="v2"):
                captured["scope"] = loop.get_knowledge_loop_config()
                yield {"event": "on_chain_end", "name": "LangGraph", "run_id": "review-root", "data": {"output": {}}}

        monkeypatch.setattr(workflow, "app", CapturingApp())
        _stub_background(monkeypatch)

        # Act：走真实stream_analysis入口传入白名单。
        await _collect(
            user_query="状态口径", datasource="orders_a", datasources=["orders_a"],
            session_id="review-scope", tenant_id=2, user_id=7, user_role="analyst",
            datasource_access={"orders_a": {"name": "orders_a"}},
        )

        # Assert：入口注入默认源和明确白名单，并在退出时恢复原配置。
        assert captured["scope"].datasource == "orders_a"
        assert captured["scope"].allowed_datasources == frozenset({"orders_a"})
        assert loop.get_knowledge_loop_config() == original
