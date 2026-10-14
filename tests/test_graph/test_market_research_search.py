"""市场研究子图联网搜索接入的直接单元与集成测试。

覆盖 features/33-web-search-workflow.md 的 33.1（显式联网路由、证据摘要、模型回退
与日志脱敏分级）与 33.2（子图与搜索服务交互集成）。直接单元用例按
spec/58-controlled-web-search.md 的固定契约使用鸭子类型假件（不复制核心定义）；
TestMarketResearchSearchIntegration 使用真实 src/search 核心接 Fake MCP 与 Fake LLM
验证端到端链路。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

# 各用例共享的真实认证身份：租户 3 / 用户 7，非匿名。
_IDENTITY_KWARGS = {"tenant_id": 3, "user_id": 7, "role": "analyst"}
# 用户当前问题：唯一允许出站的文本。
_USER_QUERY = "联网搜索新能源汽车市场近况"


# 方法作用：构造一条符合固定契约的假 SearchHit。
# Args: index - 来源序号，用于生成稳定且互不相同的字段值。
# Returns: 具备 source_id/url/title/snippet/provider/retrieved_at 的鸭子类型对象。
def _hit(index: int = 1):
    return SimpleNamespace(
        source_id=f"web:{index}",
        url=f"https://source{index}.example.com/article",
        title=f"来源标题 {index}",
        snippet=f"来源摘要正文 {index}",
        provider="public_search",
        retrieved_at=datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc),
    )


# 方法作用：构造一条符合固定契约的假 SearchResponse。
# Args: success - 是否成功；hits - 来源列表；error_code - 固定错误码；truncated - 是否截断。
# Returns: 具备 success/hits/error_code/truncated 的鸭子类型对象。
def _response(
    *,
    success: bool = True,
    hits: list[Any] | None = None,
    error_code: str = "",
    truncated: bool = False,
):
    return SimpleNamespace(
        success=success,
        hits=list(hits or []),
        error_code=error_code,
        truncated=truncated,
    )


# 方法作用：构造一条真实来源对应的期望公开引用。
# Args: index - 来源序号，与 _hit 生成的数据对应。
# Returns: 包含 source_id、locator.url、provider、UTC 检索时间和 evidence_kind 的字典。
def _citation(index: int = 1) -> dict[str, Any]:
    return {
        "source_id": f"web:{index}",
        "title": f"来源标题 {index}",
        "locator": {"url": f"https://source{index}.example.com/article"},
        "provider": "public_search",
        "retrieved_at": "2026-10-14T08:30:00+00:00",
        "evidence_kind": "search_snippet",
    }


class _FakeSearchService:
    """按 spec/58 固定契约实现的假搜索服务，记录出站查询并返回预设响应。"""

    # 方法作用：初始化假搜索服务的响应、显式意图判定与调用记录。
    # Args: response - search_public_web 的返回值；explicit - 显式联网意图判定结果；
    #       error - 非空时 search_public_web 抛出该异常。
    # Returns: 无返回值。
    def __init__(
        self,
        *,
        response: Any = None,
        explicit: bool = True,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.explicit = explicit
        self.error = error
        self.search_calls: list[str] = []

    # 方法作用：按用例配置返回显式联网意图判定。
    # Args: query - 待判定文本。
    # Returns: 用例设定的布尔值。
    def is_explicit_web_search(self, query: str) -> bool:
        return self.explicit

    # 方法作用：记录出站查询并返回预设响应或抛出预设异常。
    # Args: query - 服务端唯一允许出站的用户问题。
    # Returns: 预设的假 SearchResponse。
    async def search_public_web(self, query: str):
        self.search_calls.append(query)
        if self.error is not None:
            raise self.error
        return self.response


class _RecordingLLM:
    """记录消息与知识循环配置、返回固定内容的 Fake LLM。"""

    # 方法作用：初始化 Fake LLM 的固定输出与调用记录容器。
    # Args: content - 每次调用返回的正文；error - 非空时调用抛出该异常。
    # Returns: 无返回值。
    def __init__(self, content: str = '{"summary": "模型摘要"}', error: BaseException | None = None) -> None:
        self.content = content
        self.error = error
        self.messages: list[list[Any]] = []
        self.loop_configs: list[Any] = []
        self.configs: list[dict[str, Any]] = []

    # 方法作用：接收统一 invocation 传入的追踪配置。
    # Args: config - 调用配置。
    # Returns: 自身，链式返回与真实模型一致。
    def with_config(self, config: dict[str, Any] | None = None, **kwargs: Any) -> _RecordingLLM:
        self.configs.append(dict(config or {}))
        return self

    # 方法作用：记录本次调用的消息和当时生效的知识循环配置。
    # Args: messages - 统一 invocation 构造的消息列表；config - 运行时配置。
    # Returns: 固定内容的 AIMessage。
    async def ainvoke(self, messages: list[Any], config: dict[str, Any] | None = None, **kwargs: Any):
        from src.llm.knowledge_loop import get_knowledge_loop_config

        self.messages.append(list(messages))
        self.loop_configs.append(get_knowledge_loop_config())
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.content)


# 方法作用：绑定非匿名的真实认证身份以供用例内节点校验。
# Args: 无。
# Returns: 上下文管理器，退出后精确恢复原身份上下文。
def _authenticated_identity():
    from src.api.auth import use_identity_context
    from src.security.tenant_policy import RequestIdentity

    return use_identity_context(RequestIdentity(**_IDENTITY_KWARGS))


# 方法作用：构造市场研究节点所需的最小状态，身份与认证上下文保持一致。
# Args: overrides - 用例需要覆盖或补充的状态字段。
# Returns: 带当前问题、真实身份和能力路由的状态字典。
def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "user_query": _USER_QUERY,
        "tenant_id": _IDENTITY_KWARGS["tenant_id"],
        "user_id": _IDENTITY_KWARGS["user_id"],
        "task_plan": {"capability": "market_research"},
    }
    state.update(overrides)
    return state


# 方法作用：把子图开关与搜索服务替换为确定性假件。
# Args: monkeypatch - pytest 补丁工具；service - 假搜索服务；enabled - 总开关取值。
# Returns: 被测模块对象。
def _install_search(monkeypatch, service: Any, *, enabled: bool = True):
    import src.graph.subgraphs.market_research as module

    monkeypatch.setattr(module, "_load_search_service", lambda: service)
    monkeypatch.setattr(module, "_web_search_enabled", lambda: enabled)
    return module


# 方法作用：把任务 LLM 工厂与可用性检查替换为确定性的 Fake LLM。
# Args: monkeypatch - pytest 补丁工具；llm - Fake LLM；available - 可用性检查结果。
# Returns: 无返回值。
def _install_model(monkeypatch, llm: _RecordingLLM, *, available: bool = True) -> None:
    import src.llm.client as llm_client

    monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: available)
    monkeypatch.setattr(llm_client, "get_task_llm", lambda *args, **kwargs: llm)


class TestMarketResearchSearchGates:
    """覆盖 33.1 门禁：默认关闭零调用、显式意图、真实身份一致性。"""

    # 方法作用：验证总开关关闭时零搜索、零模型调用且返回可读失败说明。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_switch_disabled_skips_search_and_model(self, monkeypatch) -> None:
        """关闭后必须零新增外送和零新增模型调用。"""
        # Arrange
        service = _FakeSearchService(response=_response(hits=[_hit()]))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service, enabled=False)
        _install_model(monkeypatch, llm)

        # Act
        result = await module.market_research_node(_state())

        # Assert
        assert service.search_calls == []
        assert llm.messages == []
        final = result["final_response"]
        assert final["success"] is False
        assert final["status"] == "failed"
        assert final["source"] == "market_research"
        assert final["error_code"] == "WEB_SEARCH_DISABLED"
        assert "未启用" in final["error_message"]
        assert "未启用" in final["analysis"]["summary"]

    # 方法作用：验证 settings 缺失联网开关字段时默认保持关闭。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_switch_defaults_off_when_setting_missing(self, monkeypatch) -> None:
        """配置未声明总开关时必须按关闭处理，不能意外放行外送。"""
        # Arrange
        import src.config as config_module
        import src.graph.subgraphs.market_research as module

        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace())

        # Act / Assert
        assert module._web_search_enabled() is False

    # 方法作用：验证非显式联网的市场研究问法只得到友好说明，不外送。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_non_explicit_query_returns_guidance_without_outbound(self, monkeypatch) -> None:
        """既有市场研究问法在无显式联网时不得触发搜索或模型调用。"""
        # Arrange
        service = _FakeSearchService(explicit=False, response=_response(hits=[_hit()]))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state(user_query="做一份竞品研究"))

        # Assert
        assert service.search_calls == []
        assert llm.messages == []
        final = result["final_response"]
        assert final["source"] == "market_research"
        assert final["status"] == "needs_input"
        assert final["success"] is True
        assert "联网" in final["analysis"]["summary"]

    # 方法作用：验证状态身份与真实认证身份漂移时拒绝外送。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；drift - 漂移字段。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("drift", [{"user_id": 9}, {"tenant_id": 4}])
    async def test_identity_drift_between_state_and_request_rejected(self, monkeypatch, drift: dict[str, int]) -> None:
        """state 与真实认证身份不一致时必须失败关闭，不得用任一身份外送。"""
        # Arrange
        service = _FakeSearchService(response=_response(hits=[_hit()]))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state(**drift))

        # Assert
        assert service.search_calls == []
        assert llm.messages == []
        final = result["final_response"]
        assert final["success"] is False
        assert final["error_code"] == "WEB_SEARCH_IDENTITY_INVALID"

    # 方法作用：验证匿名身份（user_id=0）被拒绝。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_anonymous_identity_rejected(self, monkeypatch) -> None:
        """匿名请求不得执行联网搜索，也不得调用模型。"""
        # Arrange
        service = _FakeSearchService(response=_response(hits=[_hit()]))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        result = await module.market_research_node(_state(user_id=0))

        # Assert
        assert service.search_calls == []
        assert llm.messages == []
        assert result["final_response"]["error_code"] == "WEB_SEARCH_IDENTITY_INVALID"

    # 方法作用：验证搜索核心未部署时失败关闭而不是导入崩溃。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_missing_search_core_fails_closed(self, monkeypatch) -> None:
        """核心服务缺失时不得外送也不得调用模型，返回可读不可用说明。"""
        # Arrange
        import src.graph.subgraphs.market_research as module

        llm = _RecordingLLM()
        monkeypatch.setattr(module, "_load_search_service", lambda: None)
        monkeypatch.setattr(module, "_web_search_enabled", lambda: True)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert llm.messages == []
        final = result["final_response"]
        assert final["success"] is False
        assert final["error_code"] == "WEB_SEARCH_UNAVAILABLE"
        assert "联网" in final["error_message"]

    # 方法作用：验证通过门禁后只把当前问题原文交给核心搜索。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explicit_query_sends_exact_current_query(self, monkeypatch) -> None:
        """模型不能改写出站请求，非空历史也不能进入查询文本。"""
        # Arrange
        service = _FakeSearchService(response=_response(hits=[_hit()]))
        llm = _RecordingLLM(content='{"summary": "联网结论"}')
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert service.search_calls == [_USER_QUERY]
        assert result["final_response"]["success"] is True
        assert result["final_response"]["status"] == "success"


class TestMarketResearchEvidence:
    """覆盖 33.1 证据与引用：空证据失败关闭、错误映射、伪造引用拒绝、响应结构。"""

    # 方法作用：验证空结果时失败关闭且不调用模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_empty_hits_fail_closed_without_model(self, monkeypatch) -> None:
        """hits=[] 由研究层说明未检索到证据，不得调用模型补写结论。"""
        # Arrange
        service = _FakeSearchService(response=_response(hits=[]))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert llm.messages == []
        final = result["final_response"]
        assert final["success"] is False
        assert final["status"] == "failed"
        assert final["error_code"] == "MARKET_RESEARCH_EVIDENCE_MISSING"
        assert final["analysis"]["citations"] == []

    # 方法作用：验证搜索失败码被映射为可读公开说明。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_failure_maps_public_error_code(self, monkeypatch) -> None:
        """失败响应保留固定错误码，同时给出脱敏的可读说明且不调用模型。"""
        # Arrange
        service = _FakeSearchService(
            response=_response(success=False, error_code="WEB_SEARCH_TIMEOUT"),
        )
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert llm.messages == []
        final = result["final_response"]
        assert final["success"] is False
        assert final["error_code"] == "WEB_SEARCH_TIMEOUT"
        assert "联网" in final["error_message"]
        assert final["analysis"]["citations"] == []

    # 方法作用：验证底层异常细节不会进入响应文本。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_exception_does_not_leak_internal_details(self, monkeypatch) -> None:
        """内部异常、配置与 URL 不得出现在公开响应中。"""
        # Arrange
        service = _FakeSearchService(
            error=RuntimeError("internal-config-sentinel http://10.0.0.9/secret"),
        )
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert llm.messages == []
        serialized = json.dumps(result["final_response"], ensure_ascii=False, default=str)
        assert "internal-config-sentinel" not in serialized
        assert "10.0.0.9" not in serialized
        assert result["final_response"]["success"] is False

    # 方法作用：验证模型自报引用被拒绝，只保留真实搜索来源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_fabricated_citations_rejected(self, monkeypatch) -> None:
        """引用完全取自 SearchResponse，模型给出的来源字段一律不采信。"""
        # Arrange
        forged = {
            "summary": "模型摘要",
            "citations": [{"source_id": "forged-1", "locator": {"url": "https://evil.example"}}],
        }
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        llm = _RecordingLLM(content=json.dumps(forged, ensure_ascii=False))
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        final = result["final_response"]
        assert final["success"] is True
        assert final["analysis"]["citations"] == [_citation(1)]
        serialized = json.dumps(final, ensure_ascii=False, default=str)
        assert "forged-1" not in serialized
        assert "evil.example" not in serialized

    # 方法作用：验证成功响应携带真实引用与未抓取正文的限制说明。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_response_structure_carries_real_citations_and_limitations(self, monkeypatch) -> None:
        """analysis.citations 必须包含 source_id、locator.url、provider、UTC 时间与摘要类型。"""
        # Arrange
        hits = [_hit(1), _hit(2)]
        service = _FakeSearchService(response=_response(hits=hits))
        llm = _RecordingLLM(content='{"summary": "联网结论", "insights": ["洞察一"]}')
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        final = result["final_response"]
        assert final["source"] == "market_research"
        assert final["success"] is True
        assert final["analysis"]["citations"] == [_citation(1), _citation(2)]
        assert final["analysis"]["summary"] == "联网结论"
        assert final["analysis"]["insights"] == ["洞察一"]
        limitations = "；".join(final["analysis"]["limitations"])
        assert "未抓取网页正文" in limitations
        assert result["analysis_result"]["citations"] == [_citation(1), _citation(2)]

    # 方法作用：验证截断响应会在限制说明中明示。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_truncated_response_notes_limitation(self, monkeypatch) -> None:
        """搜索结果被截断时必须写进限制说明。"""
        # Arrange
        service = _FakeSearchService(response=_response(hits=[_hit(1)], truncated=True))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        limitations = "；".join(result["final_response"]["analysis"]["limitations"])
        assert "截断" in limitations


class TestMarketResearchIsolation:
    """覆盖 33.1 注入隔离：内部上下文不进入搜索与模型，知识工具循环被禁用。"""

    # 方法作用：验证状态中的内部哨兵字符串不进入出站查询与模型消息。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_state_sentinels_never_reach_search_or_model(self, monkeypatch) -> None:
        """历史、Schema、SQL 结果、附件、技能与知识都不得进入搜索或摘要路径。"""
        # Arrange
        sentinels = [
            "SENTINEL-HISTORY", "SENTINEL-SCHEMA", "SENTINEL-SQL", "SENTINEL-SKILL",
            "SENTINEL-KNOWLEDGE", "SENTINEL-ATTACHMENT", "SENTINEL-RESULT",
        ]
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)
        state = _state(
            conversation_history=[{"role": "user", "content": "SENTINEL-HISTORY"}],
            resolved_schema={"tables": [{"name": "SENTINEL-SCHEMA"}]},
            skill_prompt_override="SENTINEL-SKILL",
            long_term_memories_text="SENTINEL-KNOWLEDGE",
            file_ids=[1],
            attachments=[{"name": "SENTINEL-ATTACHMENT"}],
            query_result_sample=[{"col": "SENTINEL-SQL"}],
            query_result_full_count=0,
        )

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(state)

        # Assert
        assert service.search_calls == [_USER_QUERY]
        assert llm.messages, "通过门禁后必须调用摘要模型"
        message_text = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in llm.messages[0]
        )
        for sentinel in sentinels:
            assert sentinel not in message_text
        assert _USER_QUERY in message_text
        assert "来源标题 1" in message_text
        assert "外部不可信数据" in message_text
        assert result["final_response"]["success"] is True

    # 方法作用：验证调用期知识循环被禁用并在结束后恢复。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_knowledge_loop_disabled_during_call_and_restored_after(self, monkeypatch) -> None:
        """摘要调用不得携带知识工具，且不得破坏外层知识循环配置。"""
        # Arrange
        from src.llm.knowledge_loop import (
            KnowledgeLoopConfig,
            get_knowledge_loop_config,
            use_knowledge_loop,
        )

        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity(), use_knowledge_loop(
            KnowledgeLoopConfig(enabled=True, datasource="sales"),
        ):
            await module.market_research_node(_state())
            restored_inside = get_knowledge_loop_config()

        # Assert
        assert llm.loop_configs[0].enabled is False
        assert llm.loop_configs[0].identity is None
        assert restored_inside.enabled is True
        assert restored_inside.datasource == "sales"
        assert get_knowledge_loop_config().enabled is True


class TestMarketResearchFallback:
    """覆盖 33.1 模型回退与取消：确定性来源摘要、取消原样传播。"""

    # 方法作用：验证模型不可用时返回确定性来源清单而不是伪造分析。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_unavailable_returns_deterministic_source_summary(self, monkeypatch) -> None:
        """模型缺失时保留真实引用，摘要退化为有界来源清单并标注降级。"""
        # Arrange
        hits = [_hit(1), _hit(2)]
        service = _FakeSearchService(response=_response(hits=hits))
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm, available=False)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert llm.messages == []
        final = result["final_response"]
        assert final["success"] is True
        assert final["status"] == "partial"
        assert final["analysis"]["citations"] == [_citation(1), _citation(2)]
        assert "来源标题 1" in final["analysis"]["summary"]
        limitations = "；".join(final["analysis"]["limitations"])
        assert "模型" in limitations
        assert "未抓取网页正文" in limitations

    # 方法作用：验证模型异常或输出不可解析时退化为确定性来源摘要。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；scenario - 故障形态。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "scenario",
        ["exception", "invalid_json"],
    )
    async def test_model_failure_returns_deterministic_source_summary(self, monkeypatch, scenario: str) -> None:
        """模型失败不得伪造分析，也不得把失败扩散成整体失败。"""
        # Arrange
        llm = (
            _RecordingLLM(error=RuntimeError("model-down-sentinel"))
            if scenario == "exception"
            else _RecordingLLM(content="not-a-json")
        )
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        final = result["final_response"]
        assert final["success"] is True
        assert final["status"] == "partial"
        assert final["analysis"]["citations"] == [_citation(1)]
        serialized = json.dumps(final, ensure_ascii=False, default=str)
        assert "model-down-sentinel" not in serialized

    # 方法作用：验证搜索阶段取消原样传播，不触发降级。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_cancellation_propagates(self, monkeypatch) -> None:
        """取消不是失败，必须向上抛出且不得生成回退响应。"""
        # Arrange
        service = _FakeSearchService(error=asyncio.CancelledError())
        llm = _RecordingLLM()
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act / Assert
        with _authenticated_identity():
            with pytest.raises(asyncio.CancelledError):
                await module.market_research_node(_state())
        assert llm.messages == []

    # 方法作用：验证摘要模型取消原样传播，不触发降级。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_cancellation_propagates(self, monkeypatch) -> None:
        """模型调用被取消时不得改写为确定性来源摘要。"""
        # Arrange
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        llm = _RecordingLLM(error=asyncio.CancelledError())
        module = _install_search(monkeypatch, service)
        _install_model(monkeypatch, llm)

        # Act / Assert
        with _authenticated_identity():
            with pytest.raises(asyncio.CancelledError):
                await module.market_research_node(_state())


class TestMarketResearchRouting:
    """覆盖 33.1 确定性路由：显式联网关键词进入市场研究能力。"""

    # 方法作用：验证显式联网关键词稳定路由到市场研究能力。
    # Args: self - pytest 测试类实例；query - 用户问题文本。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "query",
        [
            "联网搜索新能源汽车市场",
            "联网调研一下竞品动态",
            "网上搜索供应链趋势",
            "搜索网页看看价格",
            "web search EV market",
            "Search the web for competitors",
        ],
    )
    def test_explicit_web_keywords_route_to_market_research(self, query: str) -> None:
        """明确联网关键词必须路由到市场研究子图，而不是 SQL 分析。"""
        # Arrange
        from src.graph.contracts import build_task_plan

        # Act
        plan = build_task_plan("query", query=query)

        # Assert
        assert plan.capability == "market_research"

    # 方法作用：验证既有市场研究问法仍路由到市场研究能力。
    # Args: self - pytest 测试类实例；query - 用户问题文本。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("query", ["做一份竞品研究", "市场研究", "行业研究"])
    def test_existing_market_research_keywords_still_route(self, query: str) -> None:
        """保留既有市场研究路由，显式联网边界由子图节点负责。"""
        # Arrange
        from src.graph.contracts import build_task_plan

        # Act
        plan = build_task_plan("query", query=query)

        # Assert
        assert plan.capability == "market_research"


class TestMarketResearchSearchIntegration:
    """覆盖 33.2：实际子图接真实核心服务与 Fake MCP，失败关闭与取消传播。"""

    # 方法作用：构造带必需 query 与可选 max_results 的假搜索 MCP 工具。
    # Args: handler - 异步处理函数；name - 工具完整名，必须与白名单一致。
    # Returns: 已授权形态的 LangChain StructuredTool。
    @staticmethod
    def _build_search_tool(handler, name: str = "public_search__web_search"):
        from langchain_core.tools import StructuredTool

        async def web_search(query: str, max_results: int = 8) -> str:
            return await handler(query, max_results)

        return StructuredTool.from_function(
            coroutine=web_search,
            name=name,
            description="公开网页搜索（测试假件）",
        )

    # 方法作用：装配真实核心 + Fake MCP 的集成依赖，不替换子图的加载与判定逻辑。
    # Args: monkeypatch - pytest 补丁工具；tools - 假 MCP 工具列表；enabled - 总开关；
    #       manager_calls - 可选，记录 get_all_tools 调用参数。
    # Returns: 被测模块对象。
    @staticmethod
    def _install_real_core(monkeypatch, tools: list[Any], *, enabled: bool = True, manager_calls: list[Any] | None = None):
        import src.graph.subgraphs.market_research as module
        import src.search.service as search_service
        from src.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "web_search_enabled", enabled)
        monkeypatch.setattr(settings, "web_search_mcp_tools", "public_search__web_search")
        calls = manager_calls if manager_calls is not None else []

        # 记录核心枚举工具时的身份参数，用于验证未发生多余连接或调用。
        def get_all_tools(**kwargs):
            calls.append(kwargs)
            return list(tools)

        manager = SimpleNamespace(get_all_tools=get_all_tools)
        monkeypatch.setattr(search_service, "get_mcp_client_manager", lambda *args, **kwargs: manager)
        return module

    # 方法作用：验证真实核心 + Fake MCP 下引用来自工具返回值、摘要只喂当前问题与来源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_real_core_with_fake_mcp_summarizes_real_sources(self, monkeypatch) -> None:
        """端到端链路：真实搜索解析 → 真实引用 → Fake LLM 摘要。"""
        # Arrange
        from src.config import get_settings
        from src.search.models import make_source_id

        tool_calls: list[dict[str, Any]] = []

        async def handler(query: str, max_results: int) -> str:
            tool_calls.append({"query": query, "max_results": max_results})
            return json.dumps({
                "results": [
                    {"url": "https://news.example.com/ev-market", "title": "新能源市场报告", "snippet": "市场同比增长"},
                    {"url": "https://data.example.org/industry", "title": "行业数据", "content": "产能与渗透率"},
                ]
            }, ensure_ascii=False)

        tool = self._build_search_tool(handler)
        module = self._install_real_core(monkeypatch, [tool])
        llm = _RecordingLLM(content='{"summary": "联网结论"}')
        _install_model(monkeypatch, llm)
        settings = get_settings()

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert tool_calls == [{"query": _USER_QUERY, "max_results": int(settings.web_search_max_results)}]
        final = result["final_response"]
        assert final["success"] is True
        assert final["source"] == "market_research"
        citations = final["analysis"]["citations"]
        assert [item["source_id"] for item in citations] == [
            make_source_id("https://news.example.com/ev-market"),
            make_source_id("https://data.example.org/industry"),
        ]
        assert citations[0]["locator"]["url"] == "https://news.example.com/ev-market"
        assert citations[0]["provider"] == "public_search__web_search"
        assert citations[0]["evidence_kind"] == "search_snippet"
        assert citations[0]["retrieved_at"].endswith("+00:00")
        assert "未抓取网页正文" in "；".join(final["analysis"]["limitations"])
        message_text = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in llm.messages[0]
        )
        assert _USER_QUERY in message_text
        assert "新能源市场报告" in message_text
        # 核心固定错误码不参与成功路径，也不得出现在响应里。
        assert "error_code" not in final or not final["error_code"]

    # 方法作用：验证真实显式意图判定对既有市场研究问法不外送。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_real_explicit_intent_gate_blocks_market_research_only_query(self, monkeypatch) -> None:
        """「做一份竞品研究」在真实核心下必须得到需明确联网的说明，零工具枚举。"""
        # Arrange
        manager_calls: list[Any] = []
        module = self._install_real_core(monkeypatch, [self._build_search_tool(AsyncMock())], manager_calls=manager_calls)
        llm = _RecordingLLM()
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state(user_query="做一份竞品研究"))

        # Assert
        assert manager_calls == []
        assert llm.messages == []
        final = result["final_response"]
        assert final["status"] == "needs_input"
        assert final["success"] is True
        assert "联网" in final["analysis"]["summary"]

    # 方法作用：验证真实核心工具异常时失败关闭且不泄露内部信息。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_real_core_tool_failure_fails_closed_without_model(self, monkeypatch) -> None:
        """底层工具异常只产出固定错误码与可读说明，不调用摘要模型。"""
        # Arrange
        async def handler(query: str, max_results: int) -> str:
            raise RuntimeError("core-internal-sentinel")

        module = self._install_real_core(monkeypatch, [self._build_search_tool(handler)])
        llm = _RecordingLLM()
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert llm.messages == []
        final = result["final_response"]
        assert final["success"] is False
        assert final["error_code"] == "call_failed"
        assert "联网" in final["error_message"]
        serialized = json.dumps(final, ensure_ascii=False, default=str)
        assert "core-internal-sentinel" not in serialized

    # 方法作用：验证真实核心中的取消原样传播到子图。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_real_core_cancellation_propagates(self, monkeypatch) -> None:
        """搜索被取消时不得产出任何替代响应。"""
        # Arrange
        async def handler(query: str, max_results: int) -> str:
            raise asyncio.CancelledError()

        module = self._install_real_core(monkeypatch, [self._build_search_tool(handler)])
        llm = _RecordingLLM()
        _install_model(monkeypatch, llm)

        # Act / Assert
        with _authenticated_identity():
            with pytest.raises(asyncio.CancelledError):
                await module.market_research_node(_state())
        assert llm.messages == []


# 模型、搜索、导入与身份异常使用的模拟敏感正文：只允许存在于测试桩内部，禁止落入日志。
_MODEL_SECRET = "SYNTHETIC-PRIVATE-MODEL-CONTENT"
_SEARCH_SECRET = "SYNTHETIC-PRIVATE-SEARCH-CONTENT"
_IMPORT_SECRET = "SYNTHETIC-PRIVATE-IMPORT-CONTENT"
_IDENTITY_SECRET = "SYNTHETIC-PRIVATE-IDENTITY-CONTENT"


class _RecordingLogger:
    """记录子图自身的日志事件，并按真实日志处理器语义即时还原异常堆栈。

    exc_info=True 在异常现场取当前异常（复现真实落盘行为）；(type, value, tb)
    三元组按传入的净化异常格式化，两条路径与生产处理器一致。
    """

    # 方法作用：初始化日志事件记录容器。
    # Args: 无。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    # 方法作用：记录一条事件并在调用现场即时格式化异常堆栈。
    # Args: level - 日志级别；event - 固定日志文案；fields - 结构化日志字段。
    # Returns: 无返回值。
    def _record(self, level: str, event: str, **fields: Any) -> None:
        exc_info = fields.get("exc_info")
        stack = ""
        if exc_info is True:
            # 与真实处理器一致：在该日志调用的异常处理现场取当前异常。
            stack = traceback.format_exc()
        elif isinstance(exc_info, tuple) and len(exc_info) == 3:
            stack = "".join(traceback.format_exception(*exc_info))
        self.events.append({"level": level, "event": event, "stack": stack, **fields})

    # 方法作用：记录一条 debug 日志。
    # Args: event - 固定日志文案；fields - 结构化日志字段。
    # Returns: 无返回值。
    def debug(self, event: str, **fields: Any) -> None:
        self._record("debug", event, **fields)

    # 方法作用：记录一条 info 日志。
    # Args: event - 固定日志文案；fields - 结构化日志字段。
    # Returns: 无返回值。
    def info(self, event: str, **fields: Any) -> None:
        self._record("info", event, **fields)

    # 方法作用：记录一条 warning 日志。
    # Args: event - 固定日志文案；fields - 结构化日志字段。
    # Returns: 无返回值。
    def warning(self, event: str, **fields: Any) -> None:
        self._record("warning", event, **fields)

    # 方法作用：记录一条 error 日志。
    # Args: event - 固定日志文案；fields - 结构化日志字段。
    # Returns: 无返回值。
    def error(self, event: str, **fields: Any) -> None:
        self._record("error", event, **fields)


# 方法作用：安装记录型日志器以捕获子图自身的日志事件。
# Args: monkeypatch - pytest 补丁工具。
# Returns: (记录器, 被测子图模块)。
def _install_recording_logger(monkeypatch) -> tuple[_RecordingLogger, Any]:
    import src.graph.subgraphs.market_research as module

    recorder = _RecordingLogger()
    monkeypatch.setattr(module, "logger", recorder)
    return recorder, module


# 方法作用：按级别筛选记录到的日志事件。
# Args: recorder - 记录器；level - 日志级别。
# Returns: 该级别的事件列表。
def _events(recorder: _RecordingLogger, level: str) -> list[dict[str, Any]]:
    return [event for event in recorder.events if event["level"] == level]


# 方法作用：在模型网关调用位置抛出携带模拟秘密的异常。
# Args: 无。
# Returns: 不返回，固定抛出模拟模型异常。
def _raise_model_gateway_secret() -> None:
    raise RuntimeError(_MODEL_SECRET)


class _SecretFailingLLM:
    """模拟抛出携带敏感正文异常的模型网关，抛出位置专供堆栈回溯断言。"""

    # 方法作用：接收统一 invocation 传入的追踪配置。
    # Args: config - 调用配置；kwargs - 其他追踪参数。
    # Returns: 自身，链式返回与真实模型一致。
    def with_config(self, config: dict[str, Any] | None = None, **kwargs: Any) -> _SecretFailingLLM:
        return self

    # 方法作用：在固定调用位置抛出携带模拟秘密的模型异常。
    # Args: messages - 统一 invocation 构造的消息列表；config - 运行时配置。
    # Returns: 不返回，固定抛出携带模拟秘密的异常。
    async def ainvoke(self, messages: list[Any], config: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        _raise_model_gateway_secret()


# 方法作用：在模块导入位置抛出携带模拟秘密的导入异常。
# Args: name - 待导入模块名。
# Returns: 不返回，固定抛出模拟导入异常。
def _raise_secret_import(name: str) -> Any:
    raise ImportError(f"核心模块 {name} 不可用（{_IMPORT_SECRET}）")


# 方法作用：在身份解析位置抛出携带模拟秘密的取值异常。
# Args: state - 当前状态映射。
# Returns: 不返回，固定抛出模拟解析异常。
def _raise_identity_secret(state: Any) -> Any:
    del state
    raise ValueError(f"身份取值非法（{_IDENTITY_SECRET}）")


class TestMarketResearchLogging:
    """覆盖 33.1 日志脱敏与分级：异常不落正文、单一结果摘要、取消不复读堆栈。"""

    # 方法作用：验证摘要模型异常只落固定脱敏文案、异常类与原堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_failure_stack_retained_without_secret(self, monkeypatch) -> None:
        """模拟秘密不得进入日志，原调用位置仍需可在堆栈中回溯。"""
        # Arrange
        recorder, module = _install_recording_logger(monkeypatch)
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        _install_search(monkeypatch, service)
        _install_model(monkeypatch, _SecretFailingLLM())

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert result["final_response"]["status"] == "partial"
        errors = _events(recorder, "error")
        # 单次失败只允许一次带堆栈的 error，不得逐层重复打印。
        assert len(errors) == 1
        failure = errors[0]
        assert failure["event"] == "联网研究摘要失败"
        assert failure["error_type"] == "RuntimeError"
        assert isinstance(failure["exc_info"], tuple) and len(failure["exc_info"]) == 3
        assert failure["stack"], "脱敏后仍必须保留原堆栈"
        assert _MODEL_SECRET not in failure["stack"]
        assert "RuntimeError" in failure["stack"]
        assert "退化为确定性来源清单" in failure["stack"]
        assert "_raise_model_gateway_secret" in failure["stack"]
        assert _MODEL_SECRET not in json.dumps(result, ensure_ascii=False, default=str)

    # 方法作用：验证搜索服务异常只落固定脱敏文案、异常类与原堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_failure_stack_retained_without_secret(self, monkeypatch) -> None:
        """搜索异常正文可能含问题原文或内部细节，日志不得原样落盘。"""
        # Arrange
        recorder, module = _install_recording_logger(monkeypatch)
        service = _FakeSearchService(error=RuntimeError(_SEARCH_SECRET))
        _install_search(monkeypatch, service)
        llm = _RecordingLLM()
        _install_model(monkeypatch, llm)

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert result["final_response"]["error_code"] == "WEB_SEARCH_FAILED"
        assert llm.messages == []
        errors = _events(recorder, "error")
        assert len(errors) == 1
        failure = errors[0]
        assert failure["event"] == "联网搜索调用失败"
        assert failure["error_type"] == "RuntimeError"
        assert failure["stack"], "脱敏后仍必须保留原堆栈"
        assert _SEARCH_SECRET not in failure["stack"]
        assert "返回固定错误码" in failure["stack"]
        assert "search_public_web" in failure["stack"]

    # 方法作用：验证核心导入异常同样脱敏且仍失败关闭。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_import_failure_desensitized_and_fails_closed(self, monkeypatch) -> None:
        """导入异常正文不得落盘，返回 None 让市场研究失败关闭。"""
        # Arrange
        import importlib

        recorder, module = _install_recording_logger(monkeypatch)
        monkeypatch.setattr(importlib, "import_module", _raise_secret_import)

        # Act
        loaded = module._load_search_service()

        # Assert
        assert loaded is None
        errors = _events(recorder, "error")
        assert len(errors) == 1
        failure = errors[0]
        assert failure["event"] == "联网搜索核心不可导入"
        assert failure["error_type"] == "ImportError"
        assert failure["stack"], "脱敏后仍必须保留原堆栈"
        assert _IMPORT_SECRET not in failure["stack"]
        assert "改为失败关闭" in failure["stack"]
        assert "_raise_secret_import" in failure["stack"]

    # 方法作用：验证身份解析异常不把状态原始取值写入日志。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_identity_parse_failure_desensitized(self, monkeypatch) -> None:
        """解析异常正文可能携带状态取值，只允许固定脱敏文案落盘。"""
        # Arrange
        recorder, module = _install_recording_logger(monkeypatch)
        monkeypatch.setattr("src.graph.context.resolve_request_identity", _raise_identity_secret)

        # Act
        identity = module._resolve_consistent_identity(_state())

        # Assert
        assert identity is None
        errors = _events(recorder, "error")
        assert len(errors) == 1
        failure = errors[0]
        assert failure["event"] == "联网搜索身份解析失败"
        assert failure["error_type"] == "ValueError"
        assert failure["stack"], "脱敏后仍必须保留原堆栈"
        assert _IDENTITY_SECRET not in failure["stack"]
        assert "拒绝外送" in failure["stack"]
        assert "_raise_identity_secret" in failure["stack"]

    # 方法作用：验证正常执行只在收尾保留一条聚合摘要。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_success_run_keeps_single_aggregate_summary(self, monkeypatch) -> None:
        """入口与中途结果降为 debug，INFO 只保留有运维价值的结果摘要。"""
        # Arrange
        recorder, module = _install_recording_logger(monkeypatch)
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        _install_search(monkeypatch, service)
        _install_model(monkeypatch, _RecordingLLM(content='{"summary": "联网结论"}'))

        # Act
        with _authenticated_identity():
            result = await module.market_research_node(_state())

        # Assert
        assert result["final_response"]["status"] == "success"
        infos = _events(recorder, "info")
        assert [event["event"] for event in infos] == ["市场研究子图执行完成"]
        assert infos[0]["status"] == "success" and infos[0]["citation_count"] == 1
        assert _events(recorder, "error") == []
        levels = {(event["level"], event["event"]) for event in recorder.events}
        assert ("debug", "市场研究子图执行开始") in levels
        assert ("debug", "联网搜索返回") in levels
        assert ("info", "市场研究子图执行开始") not in levels
        assert ("info", "联网搜索返回") not in levels

    # 方法作用：验证重复成功不会逐层累积 INFO 摘要。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_repeated_success_runs_keep_one_summary_each(self, monkeypatch) -> None:
        """两轮成功各只产生一条结果摘要，入口与中途摘要不重复出现。"""
        # Arrange
        recorder, module = _install_recording_logger(monkeypatch)
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        _install_search(monkeypatch, service)
        _install_model(monkeypatch, _RecordingLLM(content='{"summary": "联网结论"}'))

        # Act
        with _authenticated_identity():
            for _attempt in range(2):
                result = await module.market_research_node(_state())
                assert result["final_response"]["status"] == "success"

        # Assert
        infos = [event["event"] for event in _events(recorder, "info")]
        assert infos == ["市场研究子图执行完成", "市场研究子图执行完成"]

    # 方法作用：验证模型失败后下一轮调用恢复摘要与真实引用。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_recovers_after_model_failure(self, monkeypatch) -> None:
        """失败降级不得污染后续执行：第二次调用必须回到成功摘要。"""
        # Arrange
        recorder, module = _install_recording_logger(monkeypatch)
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        _install_search(monkeypatch, service)
        _install_model(monkeypatch, _SecretFailingLLM())

        # Act
        with _authenticated_identity():
            failed = await module.market_research_node(_state())
            _install_model(monkeypatch, _RecordingLLM(content='{"summary": "联网结论"}'))
            recovered = await module.market_research_node(_state())

        # Assert
        assert failed["final_response"]["status"] == "partial"
        assert recovered["final_response"]["status"] == "success"
        assert recovered["final_response"]["analysis"]["citations"] == [_citation(1)]
        for event in _events(recorder, "error"):
            assert _MODEL_SECRET not in event["stack"]

    # 方法作用：验证取消不是失败：原样抛出且不打印失败堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_cancellation_propagates_without_failure_stack(self, monkeypatch) -> None:
        """模型取消必须向上抛出，不得写成 error 堆栈或降级响应。"""
        # Arrange
        recorder, module = _install_recording_logger(monkeypatch)
        service = _FakeSearchService(response=_response(hits=[_hit(1)]))
        _install_search(monkeypatch, service)
        _install_model(monkeypatch, _RecordingLLM(error=asyncio.CancelledError()))

        # Act / Assert
        with _authenticated_identity():
            with pytest.raises(asyncio.CancelledError):
                await module.market_research_node(_state())
        assert _events(recorder, "error") == []
