"""通用扩展能力子图的路由、执行与产物测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
            ("请记住我偏好折线图", "memory_update"),
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

    # 方法作用：验证市场研究保留真实搜索证据并声明未抓取网页正文。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_market_research_preserves_real_evidence_and_limitations(self, monkeypatch) -> None:
        """显式联网的市场研究应保留真实来源引用，并声明只基于搜索摘要。"""
        # Arrange
        from datetime import datetime, timezone
        from types import SimpleNamespace

        import src.graph.subgraphs.market_research as module
        import src.llm.client as client_module
        from src.api.auth import use_identity_context
        from src.security.tenant_policy import RequestIdentity

        hit = SimpleNamespace(
            source_id="web:1",
            url="https://example.com/report",
            title="行业报告",
            snippet="行业摘要",
            provider="public_search",
            retrieved_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
        service = SimpleNamespace(
            is_explicit_web_search=lambda query: True,
            search_public_web=AsyncMock(return_value=SimpleNamespace(
                success=True, hits=[hit], error_code="", truncated=False,
            )),
        )
        model = SimpleNamespace(
            with_config=lambda *args, **kwargs: model,
            ainvoke=AsyncMock(return_value=SimpleNamespace(content='{"summary": "研究完成"}')),
        )
        monkeypatch.setattr(module, "_web_search_enabled", lambda: True)
        monkeypatch.setattr(module, "_load_search_service", lambda: service)
        monkeypatch.setattr(client_module, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(client_module, "get_task_llm", lambda *args, **kwargs: model)

        # Act
        with use_identity_context(RequestIdentity(tenant_id=1, user_id=5, role="user")):
            result = await module.market_research_node({
                "user_query": "联网搜索竞品动态",
                "tenant_id": 1,
                "user_id": 5,
            })

        # Assert
        assert result["final_response"]["source"] == "market_research"
        assert result["final_response"]["success"] is True
        assert result["analysis_result"]["citations"][0]["source_id"] == "web:1"
        assert "未抓取网页正文" in "；".join(result["analysis_result"]["limitations"])

    # 方法作用：验证市场研究没有可追溯引用时失败关闭且不调用模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_market_research_rejects_success_without_citations(self, monkeypatch) -> None:
        """模型摘要不能在没有真实外部证据引用时伪装成研究成功。"""
        # Arrange
        from types import SimpleNamespace

        import src.graph.subgraphs.market_research as module
        import src.llm.client as client_module
        from src.api.auth import use_identity_context
        from src.security.tenant_policy import RequestIdentity

        service = SimpleNamespace(
            is_explicit_web_search=lambda query: True,
            search_public_web=AsyncMock(return_value=SimpleNamespace(
                success=True, hits=[], error_code="", truncated=False,
            )),
        )
        get_llm = MagicMock()
        monkeypatch.setattr(module, "_web_search_enabled", lambda: True)
        monkeypatch.setattr(module, "_load_search_service", lambda: service)
        monkeypatch.setattr(client_module, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(client_module, "get_task_llm", get_llm)

        # Act
        with use_identity_context(RequestIdentity(tenant_id=1, user_id=5, role="user")):
            result = await module.market_research_node({
                "user_query": "联网搜索竞品动态",
                "tenant_id": 1,
                "user_id": 5,
            })

        # Assert
        assert result["final_response"]["success"] is False
        assert result["final_response"]["status"] == "failed"
        assert result["final_response"]["error_code"] == "MARKET_RESEARCH_EVIDENCE_MISSING"
        get_llm.assert_not_called()

    # 方法作用：验证市场研究没有授权工具时不调用模型生成无证据答案。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_market_research_without_tools_fails_closed(self, monkeypatch) -> None:
        """MCP 和 Skill 工具均为空时必须明确失败，不能返回 success=true。"""
        # Arrange
        import src.graph.nodes.mcp_agent as module
        import src.llm.client as client_module
        import src.mcp_client.client_manager as manager_module

        manager = type("Manager", (), {
            "ensure_scoped_servers": AsyncMock(),
            "get_all_tools": lambda self, **kwargs: [],
        })()
        get_llm = MagicMock()
        monkeypatch.setattr(manager_module, "get_mcp_client_manager", lambda: manager)
        monkeypatch.setattr(client_module, "get_task_llm", get_llm)
        monkeypatch.setattr(client_module, "is_task_llm_available", lambda task: True)

        # Act
        result = await module.mcp_agent_node({
            "user_query": "做竞品研究",
            "task_plan": {"capability": "market_research"},
            "skill_tools": [],
        })

        # Assert
        assert result["final_response"]["success"] is False
        assert result["final_response"]["error_code"] == "MCP_AGENT_FAILED"
        get_llm.assert_not_called()

    async def test_external_action_requires_confirmation_without_structured_request(self) -> None:
        """自然语言动作请求不得直接执行，必须返回人工确认状态。"""
        # Arrange
        from src.graph.subgraphs.action import action_node

        # Act
        result = await action_node({"user_query": "发送通知给负责人"})
        response = result["final_response"]

        # Assert
        assert response["success"] is False
        assert response["status"] == "needs_confirmation"
        assert response["data"]["action_status"] == "confirmation_required"

    # 方法作用：验证待确认响应回传可安全重放的原始动作请求。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_external_action_confirmation_returns_original_request(self) -> None:
        """前端确认时必须复用原动作参数和幂等键，只修改 confirmed。"""
        # Arrange
        from src.graph.subgraphs.action import action_node

        action_request = {
            "action_name": "notify",
            "payload": {"text": "库存不足"},
            "idempotency_key": "notify-low-stock-1",
            "confirmed": False,
            "metadata": {"source": "chat"},
        }

        # Act
        result = await action_node({"user_query": "发送库存通知", "action_request": action_request})

        # Assert
        response = result["final_response"]
        assert response["status"] == "needs_confirmation"
        assert response["data"]["action_request"] == action_request

    # 方法作用：验证结构化且已确认的动作请求可以到达动作注册表。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_external_action_dispatches_structured_request(self, monkeypatch) -> None:
        """API 传入的动作参数、确认标记和幂等键必须原样进入分派边界。"""
        # Arrange
        import src.graph.subgraphs.action as module

        dispatch = AsyncMock(return_value=type("Result", (), {
            "status": "executed", "message": "已发送", "result": {"message_id": "m-1"},
        })())
        monkeypatch.setattr(module, "get_action_registry", lambda: type("Registry", (), {"dispatch": dispatch})())

        # Act
        result = await module.action_node({
            "user_query": "发送通知",
            "action_request": {
                "action_name": "notify",
                "payload": {"channel": "email"},
                "idempotency_key": "notify-1",
                "confirmed": True,
            },
        })

        # Assert
        assert result["final_response"]["success"] is True
        request = dispatch.await_args.args[0]
        assert request.action_name == "notify"
        assert request.idempotency_key == "notify-1"
        assert request.confirmed is True

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
