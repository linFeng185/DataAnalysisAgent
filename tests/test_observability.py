"""Prometheus、LangSmith 与 Grafana 生产可观测性测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class TestPrometheusObservability:
    """覆盖功能 17.3.2 的 HTTP、错误、延迟和 LLM token 指标。"""

    # 方法作用：验证 HTTP 指标包含低基数路由、状态和延迟直方图。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_http_middleware_records_request_metrics(self) -> None:
        """请求完成后必须同时增加计数器并观测响应耗时。"""
        # Arrange
        from httpx import ASGITransport, AsyncClient
        from prometheus_client import CollectorRegistry
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        from src.observability import MetricsRegistry, PrometheusMiddleware

        # 方法作用：提供中间件测试使用的最小健康响应。
        # Args: request - Starlette 请求。
        # Returns: 固定 JSONResponse。
        async def health(request):
            del request
            return JSONResponse({"status": "ok"})

        metrics = MetricsRegistry(CollectorRegistry())
        app = Starlette(routes=[Route("/health", health)])
        app.add_middleware(PrometheusMiddleware, metrics=metrics)

        # Act
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health")
        output = metrics.render().decode("utf-8")

        # Assert
        assert response.status_code == 200
        assert 'data_agent_http_requests_total{method="GET",route="/health",status="200"} 1.0' in output
        assert "data_agent_http_request_duration_seconds_bucket" in output

    # 方法作用：验证 LLM 回调从标准 token_usage 提取 prompt/completion token。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_llm_callback_records_tokens(self) -> None:
        """模型成本指标必须按 task/model/token_type 聚合且不记录 Prompt 正文。"""
        # Arrange
        from prometheus_client import CollectorRegistry

        from src.observability import LLMMetricsCallback, MetricsRegistry

        metrics = MetricsRegistry(CollectorRegistry())
        callback = LLMMetricsCallback(metrics, task="generate_sql")
        run_id = uuid4()
        callback.on_llm_start(
            {"name": "fake-model"},
            ["sensitive prompt"],
            run_id=run_id,
            invocation_params={"model_name": "fake-model"},
        )

        # Act
        callback.on_llm_end(
            SimpleNamespace(llm_output={
                "token_usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }),
            run_id=run_id,
        )
        output = metrics.render().decode("utf-8")

        # Assert
        assert 'task="generate_sql",token_type="prompt"' in output
        assert 'task="generate_sql",token_type="completion"' in output
        assert "sensitive prompt" not in output


class TestLangSmithObservability:
    """覆盖功能 17.3.1 的显式、安全 LangSmith tracing 配置。"""

    # 方法作用：验证启用追踪时设置项目、端点、采样和输入输出隐藏开关。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_configure_langsmith_sets_safe_environment(self, monkeypatch) -> None:
        """Tracing 默认隐藏 Node 输入输出，避免 SQL、Schema 和业务数据外发。"""
        # Arrange
        from src.observability import configure_langsmith

        for key in (
            "LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT",
            "LANGSMITH_ENDPOINT", "LANGCHAIN_HIDE_INPUTS", "LANGCHAIN_HIDE_OUTPUTS",
            "LANGCHAIN_TRACING_SAMPLING_RATE",
        ):
            monkeypatch.delenv(key, raising=False)
        settings = SimpleNamespace(
            langsmith_tracing=True,
            langsmith_api_key="ls_test_secret",
            langsmith_project="agent-prod",
            langsmith_endpoint="https://api.smith.langchain.com",
            langsmith_hide_inputs=True,
            langsmith_hide_outputs=True,
            langsmith_sampling_rate=0.25,
        )

        # Act
        enabled = configure_langsmith(settings)

        # Assert
        assert enabled is True
        assert os.environ["LANGSMITH_TRACING"] == "true"
        assert os.environ["LANGSMITH_PROJECT"] == "agent-prod"
        assert os.environ["LANGCHAIN_HIDE_INPUTS"] == "true"
        assert os.environ["LANGCHAIN_HIDE_OUTPUTS"] == "true"
        assert os.environ["LANGCHAIN_TRACING_SAMPLING_RATE"] == "0.25"

    # 方法作用：验证启用 LangSmith 但未配置 Key 时立即失败。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_configure_langsmith_requires_key(self) -> None:
        """不能把“已启用但实际上不会上报”的状态伪装成可观测。"""
        from src.observability import configure_langsmith

        with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
            configure_langsmith(SimpleNamespace(
                langsmith_tracing=True,
                langsmith_api_key="",
            ))


class TestGrafanaAssets:
    """覆盖功能 17.3.3 的 Prometheus 数据源和 Grafana Dashboard。"""

    # 方法作用：验证 Grafana provisioning 和核心面板查询引用稳定指标名。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_grafana_dashboard_and_provisioning(self) -> None:
        """部署资产必须包含健康、吞吐、错误、P95 和 LLM token 面板。"""
        # Arrange
        dashboard_path = Path("observability/grafana/dashboards/data-agent.json")
        datasource_path = Path(
            "observability/grafana/provisioning/datasources/prometheus.yml",
        )
        prometheus_path = Path("observability/prometheus.yml")

        # Act
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
        dashboard_text = json.dumps(dashboard, ensure_ascii=False)
        datasource = datasource_path.read_text(encoding="utf-8")
        prometheus = prometheus_path.read_text(encoding="utf-8")

        # Assert
        assert len(dashboard["panels"]) >= 5
        assert "data_agent_http_requests_total" in dashboard_text
        assert "data_agent_http_request_duration_seconds_bucket" in dashboard_text
        assert "data_agent_llm_tokens_total" in dashboard_text
        assert "http://prometheus:9090" in datasource
        assert "host.docker.internal:8000" in prometheus
