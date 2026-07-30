"""Prometheus 指标、LLM callback 与 LangSmith 安全配置。"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from src.logging_config import get_logger

logger = get_logger(__name__)
_metrics_registry: "MetricsRegistry | None" = None
_DYNAMIC_PATH_SEGMENT = re.compile(r"/(?:\d+|[0-9a-fA-F-]{16,})(?=/|$)")


class MetricsRegistry:
    """封装应用级 Prometheus Collector，限制标签基数。"""

    # 方法作用：在指定 CollectorRegistry 中创建 HTTP 和 LLM 指标。
    # Args: self - 当前注册器；registry - 可注入的 Prometheus Registry。
    # Returns: 无返回值。
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "data_agent_http_requests_total",
            "HTTP 请求总数",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "data_agent_http_request_duration_seconds",
            "HTTP 请求响应时间",
            ("method", "route"),
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60),
            registry=self.registry,
        )
        self.llm_requests = Counter(
            "data_agent_llm_requests_total",
            "LLM 调用总数",
            ("task", "model", "status"),
            registry=self.registry,
        )
        self.llm_duration = Histogram(
            "data_agent_llm_request_duration_seconds",
            "LLM 调用响应时间",
            ("task", "model"),
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60, 120),
            registry=self.registry,
        )
        self.llm_tokens = Counter(
            "data_agent_llm_tokens_total",
            "LLM token 消耗",
            ("task", "model", "token_type"),
            registry=self.registry,
        )
        self.schema_warnings = Counter(
            "data_agent_schema_permission_warnings_total",
            "Schema 元数据权限告警总数",
            ("dialect", "operation"),
            registry=self.registry,
        )

    # 方法作用：记录一次 HTTP 请求的状态和响应耗时。
    # Args: self - 当前注册器；method/route/status - 低基数标签；duration - 秒数。
    # Returns: 无返回值。
    def record_http(self, method: str, route: str, status: int, duration: float) -> None:
        labels = (method.upper(), route, str(status))
        self.http_requests.labels(*labels).inc()
        self.http_duration.labels(method.upper(), route).observe(max(0.0, duration))

    # 方法作用：记录一次 LLM 调用结果、耗时和 token 用量。
    # Args: self - 当前注册器；task/model/status - 标签；duration - 秒数；prompt_tokens/completion_tokens - token 数。
    # Returns: 无返回值。
    def record_llm(
        self,
        *,
        task: str,
        model: str,
        status: str,
        duration: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        safe_task = str(task or "unknown")[:64]
        safe_model = str(model or "unknown")[:128]
        self.llm_requests.labels(safe_task, safe_model, status).inc()
        self.llm_duration.labels(safe_task, safe_model).observe(max(0.0, duration))
        if prompt_tokens > 0:
            self.llm_tokens.labels(safe_task, safe_model, "prompt").inc(prompt_tokens)
        if completion_tokens > 0:
            self.llm_tokens.labels(safe_task, safe_model, "completion").inc(completion_tokens)

    # 方法作用：记录一次 Schema 元数据权限告警并限制标签基数。
    # Args: self - 当前注册器；dialect - 数据库方言；operation - 元数据操作。
    # Returns: 无返回值。
    def record_schema_warning(self, dialect: str, operation: str) -> None:
        self.schema_warnings.labels(
            str(dialect or "unknown")[:32],
            str(operation or "unknown")[:32],
        ).inc()

    # 方法作用：将当前指标编码为 Prometheus 文本格式。
    # Args: self - 当前注册器。
    # Returns: Prometheus exposition bytes。
    def render(self) -> bytes:
        return generate_latest(self.registry)


class LLMMetricsCallback(BaseCallbackHandler):
    """仅采集模型名、耗时和 token，不保存 Prompt 或响应正文。"""

    # 方法作用：初始化指定任务的 LLM 指标回调。
    # Args: self - 当前回调；metrics - 指标注册器；task - 节点任务名。
    # Returns: 无返回值。
    def __init__(self, metrics: MetricsRegistry, task: str) -> None:
        self.metrics = metrics
        self.task = task
        self._runs: dict[UUID, tuple[float, str]] = {}

    # 方法作用：记录 LLM 调用起点和低基数模型名，不保留 prompts。
    # Args: self - 当前回调；serialized/prompts - LangChain 参数；run_id - 调用 ID；kwargs - invocation 参数。
    # Returns: 无返回值。
    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del prompts
        invocation = dict(kwargs.get("invocation_params", {}) or {})
        model = str(
            invocation.get("model_name")
            or invocation.get("model")
            or serialized.get("name")
            or "unknown"
        )
        self._runs[run_id] = (time.monotonic(), model[:128])

    # 方法作用：从 LLMResult 读取 token_usage 并记录成功指标。
    # Args: self - 当前回调；response - LangChain LLMResult；run_id - 调用 ID；kwargs - 扩展字段。
    # Returns: 无返回值。
    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del kwargs
        started_at, model = self._runs.pop(run_id, (time.monotonic(), "unknown"))
        prompt_tokens, completion_tokens = _extract_token_usage(response)
        self.metrics.record_llm(
            task=self.task,
            model=model,
            status="success",
            duration=time.monotonic() - started_at,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    # 方法作用：记录 LLM 异常次数和失败前耗时，不记录异常正文。
    # Args: self - 当前回调；error - 模型异常；run_id - 调用 ID；kwargs - 扩展字段。
    # Returns: 无返回值。
    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del error, kwargs
        started_at, model = self._runs.pop(run_id, (time.monotonic(), "unknown"))
        self.metrics.record_llm(
            task=self.task,
            model=model,
            status="error",
            duration=time.monotonic() - started_at,
        )


class PrometheusMiddleware:
    """记录所有 HTTP 请求的低基数 Prometheus 指标。"""

    # 方法作用：保存下游 ASGI 应用和指标注册器。
    # Args: self - 当前中间件；app - 下游应用；metrics - 指标注册器。
    # Returns: 无返回值。
    def __init__(self, app: Any, *, metrics: MetricsRegistry | None = None) -> None:
        self.app = app
        self.metrics = metrics or get_metrics_registry()

    # 方法作用：包裹 HTTP 响应并记录请求数、状态码和耗时。
    # Args: self - 当前中间件；scope/receive/send - ASGI 协议参数。
    # Returns: 无返回值。
    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") == "/api/v1/metrics":
            await self.app(scope, receive, send)
            return
        started_at = time.monotonic()
        status_code = 500

        # 方法作用：捕获响应状态并原样转发 ASGI 消息。
        # Args: message - 下游响应消息。
        # Returns: 无返回值。
        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = _route_label(scope)
            self.metrics.record_http(
                str(scope.get("method", "UNKNOWN")),
                route,
                status_code,
                time.monotonic() - started_at,
            )


# 方法作用：获取进程内共享的 Prometheus 指标注册器。
# Args: 无。
# Returns: 单例 MetricsRegistry。
def get_metrics_registry() -> MetricsRegistry:
    global _metrics_registry
    if _metrics_registry is None:
        _metrics_registry = MetricsRegistry()
    return _metrics_registry


# 方法作用：给现有 ChatModel 附加指标 callback 并保持对象身份不变。
# Args: model - Provider 返回模型；task - 节点任务名。
# Returns: 原模型对象。
def attach_llm_metrics(model: Any, task: str) -> Any:
    try:
        callback = LLMMetricsCallback(get_metrics_registry(), task)
        callbacks = list(getattr(model, "callbacks", None) or [])
        callbacks.append(callback)
        model.callbacks = callbacks
    except Exception as exc:
        logger.warning("LLM 指标回调附加失败", task=task, error=str(exc), exc_info=True)
    return model


# 方法作用：按 Settings 显式配置 LangSmith 环境并默认隐藏输入输出。
# Args: settings - LangSmith 配置来源。
# Returns: 实际启用 tracing 时返回 True。
def configure_langsmith(settings: Any) -> bool:
    enabled = bool(getattr(settings, "langsmith_tracing", False))
    if not enabled:
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.info("LangSmith tracing 已禁用")
        return False
    api_key = str(getattr(settings, "langsmith_api_key", "") or "").strip()
    if not api_key:
        raise ValueError("启用 LangSmith tracing 必须配置 LANGSMITH_API_KEY")
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = api_key
    os.environ["LANGSMITH_PROJECT"] = str(
        getattr(settings, "langsmith_project", "data-analysis-agent")
    )
    endpoint = str(getattr(settings, "langsmith_endpoint", "") or "").strip()
    if endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = endpoint
    os.environ["LANGCHAIN_HIDE_INPUTS"] = str(
        bool(getattr(settings, "langsmith_hide_inputs", True))
    ).lower()
    os.environ["LANGCHAIN_HIDE_OUTPUTS"] = str(
        bool(getattr(settings, "langsmith_hide_outputs", True))
    ).lower()
    sampling_rate = float(getattr(settings, "langsmith_sampling_rate", 1.0))
    if not 0.0 <= sampling_rate <= 1.0:
        raise ValueError("LANGSMITH_SAMPLING_RATE 必须在 0 到 1 之间")
    os.environ["LANGCHAIN_TRACING_SAMPLING_RATE"] = str(sampling_rate)
    logger.info(
        "LangSmith tracing 已启用",
        project=os.environ["LANGSMITH_PROJECT"],
        sampling_rate=sampling_rate,
        hide_inputs=os.environ["LANGCHAIN_HIDE_INPUTS"],
        hide_outputs=os.environ["LANGCHAIN_HIDE_OUTPUTS"],
    )
    return True


# 方法作用：返回 Prometheus 文本响应所需的内容和媒体类型。
# Args: 无。
# Returns: 指标 bytes 和 Prometheus CONTENT_TYPE。
def render_metrics() -> tuple[bytes, str]:
    return get_metrics_registry().render(), CONTENT_TYPE_LATEST


# 方法作用：从标准或兼容 LLMResult 中提取 prompt/completion token。
# Args: response - LangChain LLMResult 或测试替身。
# Returns: prompt token 与 completion token。
def _extract_token_usage(response: Any) -> tuple[int, int]:
    llm_output = dict(getattr(response, "llm_output", None) or {})
    usage = dict(llm_output.get("token_usage", {}) or llm_output.get("usage", {}) or {})
    prompt = int(usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0)
    completion = int(
        usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    )
    return prompt, completion


# 方法作用：优先使用框架路由模板，回退时替换动态 ID 以限制标签基数。
# Args: scope - ASGI HTTP scope。
# Returns: 适合作为 Prometheus 标签的路由。
def _route_label(scope: dict) -> str:
    route = scope.get("route")
    path = str(getattr(route, "path", "") or scope.get("path", "unknown"))
    return _DYNAMIC_PATH_SEGMENT.sub("/{id}", path)[:256]
