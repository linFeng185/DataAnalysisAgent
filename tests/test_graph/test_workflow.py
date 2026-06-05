"""LangGraph 编排引擎测试 — 状态、路由、Node、e2e。"""

from __future__ import annotations

import asyncio

import pytest


class TestAnalysisState:
    """4.1.1"""

    def test_minimal(self):
        from src.graph.state import AnalysisState
        s: AnalysisState = {"user_query": "查订单", "datasource": "ch"}
        assert s["user_query"] == "查订单"

    def test_defaults(self):
        from src.graph.state import AnalysisState
        s: AnalysisState = {}
        assert s.get("generated_sql") is None


class TestConditionalRouting:
    """4.1.3-6"""

    def test_layer3_security_block(self):
        from src.graph.workflow import after_layer3
        assert after_layer3({"validation_errors": [{"type": "security_block"}]}) == "build_response"

    def test_layer3_syntax_retry(self):
        from src.graph.workflow import after_layer3
        assert after_layer3({"validation_errors": [{"type": "syntax_error"}]}) == "generate_sql"

    def test_layer3_pass(self):
        from src.graph.workflow import after_layer3
        assert after_layer3({"validation_errors": []}) == "layer4_explain"

    def test_layer4_retry(self):
        from src.graph.workflow import after_layer4
        assert after_layer4({"explain_errors": [{}], "retry_count": 0}) == "generate_sql"

    def test_layer4_exhausted(self):
        from src.graph.workflow import after_layer4
        assert after_layer4({"explain_errors": [{}], "retry_count": 3}) == "build_response"

    def test_layer4_pass(self):
        from src.graph.workflow import after_layer4
        assert after_layer4({"explain_errors": []}) == "execute_sql"

    def test_retry_with_error(self):
        from src.graph.workflow import should_retry
        assert should_retry({"execution_error": "t", "retry_count": 0}) == "generate_sql"

    def test_retry_exhausted(self):
        from src.graph.workflow import should_retry
        assert should_retry({"execution_error": "t", "retry_count": 3}) == "build_response"

    def test_retry_no_error(self):
        from src.graph.workflow import should_retry
        assert should_retry({"execution_error": "", "retry_count": 0}) == "build_response"

    def test_intent_file(self):
        from src.graph.workflow import route_by_intent
        assert route_by_intent({"intent": "file_analysis"}) == "mcp_agent"

    def test_intent_normal(self):
        from src.graph.workflow import route_by_intent
        assert route_by_intent({"intent": "query"}) == "retrieve_schema"


class TestIntentClassification:
    """4.2"""

    @pytest.mark.parametrize("q,exp", [
        ("为什么GMV下降", "attribution"),
        ("近30天趋势", "trend"),
        ("各品类排名", "aggregation"),
        ("表结构", "metadata"),
        ("上传CSV", "file_analysis"),
        ("查订单数", "query"),
        ("你好", "chat"),
    ])
    def test_classify(self, q, exp):
        from src.graph.nodes.classify_intent import classify_intent_node
        r = asyncio.run(classify_intent_node({"user_query": q}))
        assert r["intent"] == exp


class TestNodes:
    """4.3-4.10"""

    def test_retrieve_schema_empty(self):
        from src.graph.nodes.retrieve_schema import retrieve_schema_node
        r = asyncio.run(retrieve_schema_node({}))
        assert r["relevant_tables"] == []

    def test_generate_sql(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")  # 使用模板回退，避免真实 API 调用
        from src.graph.nodes.generate_sql import generate_sql_node
        r = asyncio.run(generate_sql_node({"relevant_tables": [{"name": "t", "columns": []}]}))
        assert "t" in r["generated_sql"]

    def test_layer3_pass(self):
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "SELECT 1"}))
        assert r["sql_valid"] is True

    def test_layer3_block_drop(self):
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "DROP TABLE users"}))
        assert r["sql_valid"] is False
        assert r["validation_errors"][0]["type"] == "security_block"

    def test_layer3_block_delete(self):
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "DELETE FROM t"}))
        assert r["sql_valid"] is False

    def test_execute_sql(self):
        """无数据源时返回空结果 + 错误提示。"""
        from src.graph.nodes.execute_sql import execute_sql_node
        r = asyncio.run(execute_sql_node({"datasource": "nonexistent"}))
        assert r["execution_error"] != ""  # 应该有错误提示
        assert "query_result_sample" in r

    def test_analyze_result(self):
        from src.graph.nodes.analyze_result import analyze_result_node
        r = asyncio.run(analyze_result_node({}))
        assert "summary" in r["analysis_result"]

    def test_generate_chart(self):
        from src.graph.nodes.generate_chart import generate_chart_node
        assert asyncio.run(generate_chart_node({}))["chart_config"]["type"] == "bar"

    def test_build_response_ok(self):
        from src.graph.nodes.build_response import build_response_node
        r = asyncio.run(build_response_node({"user_query": "q"}))
        assert r["final_response"]["success"] is True

    def test_build_response_error(self):
        from src.graph.nodes.build_response import build_response_node
        r = asyncio.run(build_response_node({"validation_errors": [{}]}))
        assert r["final_response"]["success"] is False


class TestE2E:
    """集成测试"""

    def test_workflow_compiles(self):
        from src.graph.workflow import build_workflow
        assert build_workflow() is not None

    def test_simple_query(self, monkeypatch):
        """完整链路: 无数据源 → 返回错误提示。"""
        monkeypatch.setenv("OPENAI_API_KEY", "")  # 使用模板回退避免 recursion
        from src.graph.workflow import app
        r = asyncio.run(app.ainvoke({
            "user_query": "查昨天订单",
            "datasource": "clickhouse_prod",
        }))
        assert "final_response" in r

    def test_dangerous_sql_blocked_at_node_level(self):
        """DROP 语句在 layer3_validate Node 被拦截 (已在 TestNodes 覆盖)。"""
        from src.graph.nodes.layer3_validate import layer3_validate_node
        r = asyncio.run(layer3_validate_node({"generated_sql": "DROP TABLE users"}))
        assert r["sql_valid"] is False

    def test_retry_path(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        from src.graph.workflow import app
        r = asyncio.run(app.ainvoke({
            "user_query": "查",
            "generated_sql": "SELECT bad",
            "retry_count": 0,
        }))
        assert "final_response" in r
