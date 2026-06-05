"""10.1 LLM 客户端 + 4.4/4.8 Node 集成测试 — 测工厂/路由/降级/回退。"""

from __future__ import annotations

import asyncio

import pytest


class TestLLMFactory:
    """10.1.1-4"""

    def test_openai_creates(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder")
        from src.llm.client import get_openai_llm
        assert get_openai_llm(model="gpt-4o-mini", temperature=0) is not None

    def test_router_default(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder")
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        from src.llm.client import get_llm
        assert get_llm() is not None

    def test_cheap_llm(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder")
        from src.llm.client import get_cheap_llm
        assert get_cheap_llm() is not None

    def test_not_available_no_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        from src.llm.client import is_llm_available
        assert is_llm_available() is False

    def test_available_with_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        from src.llm.client import is_llm_available
        assert is_llm_available() is True


class TestGenerateSQLFallback:
    """4.4 无 API Key 时退回模板。"""

    def test_template_fallback(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        from src.graph.nodes.generate_sql import generate_sql_node
        r = asyncio.run(generate_sql_node({
            "user_query": "查订单", "relevant_tables": [{"name": "orders", "columns": []}],
            "dialect": "clickhouse", "retry_count": 0,
        }))
        assert "orders" in r["generated_sql"]

    def test_retry_mode(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        from src.graph.nodes.generate_sql import generate_sql_node
        r = asyncio.run(generate_sql_node({
            "relevant_tables": [{"name": "t", "columns": []}], "retry_count": 2,
            "validation_errors": [{}],
        }))
        assert "fix" in r["generated_sql"].lower() or "t" in r["generated_sql"]


class TestAnalyzeResultFallback:
    """4.8 无 LLM 时规则分析。"""

    def test_rule_with_data(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        from src.graph.nodes.analyze_result import analyze_result_node
        r = asyncio.run(analyze_result_node({
            "query_result_sample": [
                {"category": "电子", "sales": 128000},
                {"category": "家居", "sales": 102000},
            ],
            "intent": "aggregation",
        }))
        a = r["analysis_result"]
        assert a["summary"]
        assert len(a["insights"]) > 0

    def test_empty(self):
        from src.graph.nodes.analyze_result import analyze_result_node
        r = asyncio.run(analyze_result_node({"query_result_sample": []}))
        assert "无数据" in r["analysis_result"]["summary"]


class TestPrompts:
    """10.2"""

    def test_all_dialects(self):
        from src.llm.prompts import get_dialect_cheatsheet
        assert get_dialect_cheatsheet("clickhouse")
        assert get_dialect_cheatsheet("mysql")
        assert get_dialect_cheatsheet("postgres")
