"""通用扩展能力子图的路由、执行与产物测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver


class TestCapabilityRouting:
    """覆盖功能 4.1.17、19.22 的能力识别和主图拓扑。"""

    @pytest.mark.parametrize(
        ("query", "capability"),
        [
            ("预测未来 3 天销售额", "forecast"),
            ("生成订单月报", "report"),
            ("做一份竞品研究", "market_research"),
            ("发送通知给负责人", "external_action"),
        ],
    )
    def test_task_plan_detects_extension_capability(self, query: str, capability: str) -> None:
        """稳定关键词必须映射到专用能力，而不是退化为 SQL 或闲聊。"""
        # Arrange
        from src.graph.contracts import build_task_plan

        # Act
        plan = build_task_plan("query", query=query)

        # Assert
        assert plan.capability == capability

    def test_post_analysis_routes_forecast_and_report(self) -> None:
        """预测和报告必须复用 SQL 分析后再进入各自能力子图。"""
        # Arrange
        from src.graph.workflow import after_analyze_result

        forecast_state = {"task_plan": {"capability": "forecast"}}
        report_state = {"task_plan": {"capability": "report"}}

        # Act / Assert
        assert after_analyze_result(forecast_state) == "forecast_subgraph"
        assert after_analyze_result(report_state) == "report_subgraph"
        assert after_analyze_result({"task_plan": {"capability": "sql_analysis"}}) == "generate_chart"

    async def test_compiled_graph_contains_all_capability_subgraphs(self) -> None:
        """五类能力必须作为真实图节点存在并统一进入 build_response。"""
        # Arrange
        import src.graph.workflow as workflow_module

        # Act
        with patch(
            "src.memory.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=MemorySaver()),
        ):
            graph = await workflow_module.build_workflow()
        edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}

        # Assert
        for node in (
            "file_analysis_subgraph",
            "market_research_subgraph",
            "forecast_subgraph",
            "report_subgraph",
            "action_subgraph",
        ):
            assert (node, "build_response") in edges


class TestCapabilityExecution:
    """覆盖预测、市场研究和外部动作子图的业务输出。"""

    async def test_forecast_subgraph_builds_forecast_artifact(self) -> None:
        """有序时间序列应生成预测、区间、回测信息和 forecast Artifact。"""
        # Arrange
        from src.graph.artifacts import build_analysis_artifact
        from src.graph.subgraphs.forecast import forecast_node

        state = {
            "user_query": "预测未来 2 天销售额",
            "datasource": "sales",
            "task_plan": {"capability": "forecast"},
            "query_result_sample": [
                {"date": f"2026-07-{day:02d}", "sales": day * 10}
                for day in range(1, 9)
            ],
            "analysis_result": {"summary": "销售额持续变化", "insights": []},
        }

        # Act
        result = await forecast_node(state)
        response = {
            "source": "sql_query",
            "status": "success",
            "analysis": result["analysis_result"],
            "data": state["query_result_sample"],
            "chart": result["chart_config"],
        }
        artifact = build_analysis_artifact(state, response)

        # Assert
        assert len(result["analysis_result"]["forecast"]["predictions"]) == 2
        assert result["chart_config"]["type"] == "line"
        assert artifact["kind"] == "forecast"
        assert artifact["data"]["model_card"]["leakage_check"] == "passed"

    async def test_market_research_subgraph_preserves_failure_and_limitations(self, monkeypatch) -> None:
        """市场研究应保留工具真实成败，并声明只使用当前授权证据。"""
        # Arrange
        import src.graph.subgraphs.market_research as module

        monkeypatch.setattr(
            module,
            "mcp_agent_node",
            AsyncMock(return_value={
                "final_response": {
                    "success": True,
                    "status": "success",
                    "source": "mcp_agent",
                    "analysis": {"summary": "研究完成"},
                },
                "analysis_result": {"summary": "研究完成"},
            }),
        )

        # Act
        result = await module.market_research_node({"user_query": "竞品研究"})

        # Assert
        assert result["final_response"]["source"] == "market_research"
        assert "授权" in result["analysis_result"]["limitations"][0]

    async def test_external_action_requires_confirmation_without_structured_request(self) -> None:
        """自然语言动作请求不得直接执行，必须返回人工确认状态。"""
        # Arrange
        from src.graph.subgraphs.action import action_node

        # Act
        result = await action_node({"user_query": "发送通知给负责人"})
        response = result["final_response"]

        # Assert
        assert response["success"] is True
        assert response["status"] == "needs_confirmation"
        assert response["data"]["action_status"] == "confirmation_required"

    async def test_capability_failure_keeps_safe_specific_message(self, monkeypatch) -> None:
        """统一出口不得把能力子图的安全错误文案覆盖成笼统直接回答失败。"""
        # Arrange
        import src.memory.history_store as history_module
        from src.graph.nodes.build_response import build_response_node

        monkeypatch.setattr(
            history_module,
            "get_history_store",
            lambda: type("Store", (), {"add": AsyncMock()})(),
        )
        state = {
            "user_query": "预测未来3天",
            "final_response": {
                "success": False,
                "status": "failed",
                "source": "forecast",
                "user_query": "预测未来3天",
                "error_code": "FORECAST_INPUT_INVALID",
                "error_message": "当前结果不满足预测所需的时间序列条件",
                "analysis": {},
                "chart": {},
            },
        }

        # Act
        result = await build_response_node(state)

        # Assert
        assert result["final_response"]["error_message"] == "当前结果不满足预测所需的时间序列条件"
