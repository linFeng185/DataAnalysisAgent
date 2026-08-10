"""10.1 LLM 客户端 + 4.4/4.8 Node 集成测试 — 测工厂/路由/降级/回退。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace


# 方法作用：构造 LLM 测试需要的完整最小 Settings。
# Args: overrides - 需要覆盖的配置字段。
# Returns: 可注入 AppContext 的配置对象。
def _llm_settings(**overrides):
    values = {
        "multi_tenant": False,
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "llm_temperature": 0.0,
        "llm_max_tokens": 4096,
        "llm_timeout": 60,
        "openai_api_key": "",
        "openai_base_url": "",
        "anthropic_api_key": "",
        "cheap_llm_model": "gpt-4o-mini",
        "local_llm_model": "",
        "local_llm_base_url": "",
        "local_llm_api_key": "local",
        "local_llm_timeout": 15,
        "llm_remote_tasks": "generate_sql",
        "llm_allow_remote_fallback": False,
        "max_retry_count": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# 方法作用：在测试期间绑定使用指定 Settings 的独立 AppContext。
# Args: overrides - 需要覆盖的配置字段。
# Returns: 绑定期间的 AppContext 上下文管理器。
@contextmanager
def _llm_context(**overrides):
    from src.app_context import AppContext, use_app_context

    with use_app_context(AppContext(_llm_settings(**overrides))) as context:
        yield context


class TestLLMFactory:
    """10.1.1-4"""

    def test_openai_creates(self, monkeypatch):
        del monkeypatch
        from src.llm.client import get_openai_llm
        with _llm_context(openai_api_key="sk-placeholder"):
            assert get_openai_llm(
                model="gpt-4o-mini",
                temperature=0,
                api_key="tenant-placeholder",
            ) is not None

    def test_router_requires_tenant_selection(self):
        import pytest

        from src.llm.client import get_llm

        with _llm_context(openai_api_key="sk-placeholder"):
            with pytest.raises(RuntimeError, match="管理后台"):
                get_llm()

    def test_cheap_llm_requires_local_or_tenant_model(self):
        import pytest

        from src.llm.client import get_cheap_llm

        with _llm_context(openai_api_key="sk-placeholder"):
            with pytest.raises(RuntimeError, match="可用"):
                get_cheap_llm()

    # 方法作用：验证低成本模型沿用已配置 Provider 而非硬编码 OpenAI 工厂。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cheap_llm_uses_configured_provider(self, monkeypatch):
        """Anthropic 等 Provider 的 cheap model 必须走统一路由。"""
        from types import SimpleNamespace

        import src.llm.client as client_module

        captured = {}
        expected = object()
        monkeypatch.setattr(
            client_module,
            "get_settings",
            lambda: SimpleNamespace(llm_provider="anthropic", cheap_llm_model="claude-haiku"),
        )

        # 方法作用：捕获低成本模型路由参数。
        # Args: kwargs - get_llm 接收的模型路由参数。
        # Returns: 预设模型对象。
        def fake_get_task_llm(task, **kwargs):
            captured["task"] = task
            captured.update(kwargs)
            return expected

        monkeypatch.setattr(client_module, "get_task_llm", fake_get_task_llm)

        result = client_module.get_cheap_llm()

        assert result is expected
        assert captured["task"] == "context_summary"
        assert captured["reasoning"] is False

    def test_not_available_no_key(self, monkeypatch):
        del monkeypatch
        from src.llm.client import is_llm_available
        with _llm_context(openai_api_key=""):
            assert is_llm_available() is False

    def test_available_with_key(self, monkeypatch):
        del monkeypatch
        from src.llm.client import is_llm_available
        with _llm_context(openai_api_key="sk-test"):
            assert is_llm_available() is False

    def test_available_with_tenant_selection_without_global_key(self):
        """租户管理后台凭证有效时，不应依赖全局 API Key。"""
        from src.llm.client import is_llm_available
        from src.llm.tenant_config import TenantLLMSelection, use_tenant_llm_selection

        selection = TenantLLMSelection(
            tenant_id=7,
            connection_id=11,
            connection_name="primary",
            provider_code="deepseek",
            protocol="openai_compatible",
            model_catalog_id=21,
            model_id="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="tenant-key",
        )
        with _llm_context(openai_api_key=""):
            with use_tenant_llm_selection(selection):
                assert is_llm_available() is True


class TestGenerateSQLFallback:
    """4.4 无 API Key 时退回模板。"""

    def test_template_fallback(self, monkeypatch):
        del monkeypatch
        from src.graph.nodes.generate_sql import generate_sql_node
        with _llm_context(openai_api_key=""):
            r = asyncio.run(generate_sql_node({
                "user_query": "查订单数", "relevant_tables": [{"name": "orders", "columns": []}],
                "dialect": "clickhouse", "retry_count": 0,
            }, {}))
        assert "COUNT(*)" in r["generated_sql"].upper()
        assert "orders" in r["generated_sql"]

    def test_retry_mode(self, monkeypatch):
        del monkeypatch
        from src.graph.nodes.generate_sql import generate_sql_node
        with _llm_context(openai_api_key=""):
            r = asyncio.run(generate_sql_node({
                "relevant_tables": [{"name": "t", "columns": []}], "retry_count": 2,
                "validation_errors": [{}],
            }, {}))
        assert r["generated_sql"] == ""
        assert "LLM" in r["execution_error"]


class TestAnalyzeResultFallback:
    """4.8 无 LLM 时规则分析。"""

    def test_rule_with_data(self, monkeypatch):
        del monkeypatch
        from src.graph.nodes.analyze_result import analyze_result_node
        with _llm_context(openai_api_key=""):
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
