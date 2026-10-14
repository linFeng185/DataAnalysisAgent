"""市场研究独立能力子图：受控联网搜索与证据摘要。

设计见 spec/58-controlled-web-search.md 第 4 节：先独立校验总开关、显式联网意图与
真实身份一致性，再调用受控搜索核心；摘要只接收当前问题与有界 SearchHit 证据，
引用完全取自真实 SearchResponse，模型失效时退化为确定性来源摘要。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, StateGraph

from src.graph.nodes.mcp_agent import _mcp_standard_output
from src.graph.outcome import public_error_message
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)

# 摘要调用使用的注册 Prompt 与统一任务标识。
_SUMMARY_PROMPT_ID = "web.research"
_SUMMARY_LLM_TASK = "market_research"
# 搜索核心的稳定模块路径：延迟加载，核心未部署时不拖垮整图导入。
_SEARCH_SERVICE_MODULE = "src.search.service"
# 进入模型摘要的证据上限：条数、单条与总量都设有界，防止不可信文本放大。
_MAX_EVIDENCE_HITS = 8
_MAX_EVIDENCE_PER_HIT_CHARS = 1200
_MAX_EVIDENCE_TOTAL_CHARS = 6000
# 模型给出的洞察与限制只做展示：越界截断而不是拒收整次摘要。
_MAX_MODEL_ITEMS = 8
_MAX_MODEL_ITEM_CHARS = 300
# 确定性回退摘要的条数、标题与总长上限。
_MAX_FALLBACK_ITEMS = 5
_MAX_FALLBACK_TITLE_CHARS = 80
_MAX_FALLBACK_CHARS = 1200

_LIMITATION_NO_PAGE_BODY = "研究结论仅基于搜索返回的摘要片段，未抓取网页正文。"
_LIMITATION_UNTRUSTED_SOURCES = "来源为外部网页数据，仅供参考，未经独立核验。"
_LIMITATION_TRUNCATED = "本次搜索结果已被截断，仅包含部分来源。"
# 既有市场研究问法在没有显式联网要求时的友好说明：不失败，也不外送。
_EXPLICIT_REQUIRED_MESSAGE = (
    "为避免未经授权的对外联网，市场研究需要您明确要求联网后才会执行。"
    "请明确说明需要联网搜索（例如「联网搜索新能源汽车市场」）后重试。"
)
_EXPLICIT_REQUIRED_LIMITATION = "本次未执行任何联网搜索，也未向外部服务发送请求。"

# 联网搜索固定错误码到公开说明的映射：键为规范小写形态，与核心 models 的固定码一致；
# 未知错误码一律走兜底文案，不泄露内部配置、异常正文或工具细节。
_WEB_SEARCH_PUBLIC_MESSAGES = {
    "disabled": "联网搜索功能未启用，市场研究暂时无法执行",
    "no_explicit_intent": "为避免未经授权的对外联网，市场研究需要您明确要求联网后才会执行",
    "invalid_query": "联网搜索问题不符合要求，请调整后重试",
    "unauthorized": "当前请求身份未通过联网搜索校验，已停止向外部发送请求",
    "no_tool": "当前没有可用的联网搜索服务，市场研究暂时无法执行",
    "timeout": "联网搜索超时，请稍后重试",
    "call_failed": "联网搜索未能完成，请稍后重试或改用其他分析方式",
    "response_too_large": "搜索结果超出处理上限，已停止解析",
    "invalid_response": "搜索服务返回的内容无法解析，请稍后重试",
    "no_usable_sources": "搜索结果不含可追溯来源，无法生成可靠结论",
}
# 子图自有门禁码归一到核心固定码，保证同一失败在公开层只有一种说明。
_WEB_SEARCH_LOCAL_CODE_ALIASES = {
    "WEB_SEARCH_DISABLED": "disabled",
    "WEB_SEARCH_UNAVAILABLE": "no_tool",
    "WEB_SEARCH_IDENTITY_INVALID": "unauthorized",
    "WEB_SEARCH_QUERY_INVALID": "invalid_query",
    "WEB_SEARCH_TIMEOUT": "timeout",
    "WEB_SEARCH_FAILED": "call_failed",
}
_WEB_SEARCH_FALLBACK_MESSAGE = "联网搜索未能完成，请稍后重试或改用其他分析方式"


# 方法作用：读取联网搜索总开关，配置缺失时保持关闭。
# Args: 无。
# Returns: 总开关为真时返回 True；字段缺失一律按关闭处理。
def _web_search_enabled() -> bool:
    """默认关闭：只有显式启用配置才允许联网搜索。"""
    from src.config import get_settings

    enabled = bool(getattr(get_settings(), "web_search_enabled", False))
    logger.debug("联网搜索总开关读取", enabled=enabled)
    return enabled


# 方法作用：延迟加载受控搜索核心，未部署时返回 None 而不是让整图导入失败。
# Args: 无。
# Returns: src.search.service 模块；不可导入时返回 None。
def _load_search_service() -> Any | None:
    """核心服务是独立交付单元，缺失时市场研究必须失败关闭。"""
    from importlib import import_module

    try:
        return import_module(_SEARCH_SERVICE_MODULE)
    except ImportError as exc:
        # 导入异常正文可能携带内部路径；沿用 sql_rewriter 的安全三元组范式，
        # 只保留固定脱敏文案、原始异常类名与真实调用堆栈。
        safe_error = RuntimeError("联网搜索核心不可导入，改为失败关闭")
        logger.error(
            "联网搜索核心不可导入",
            module=_SEARCH_SERVICE_MODULE,
            error_type=type(exc).__name__,
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return None


# 方法作用：把固定错误码映射为脱敏的用户可见说明。
# Args: error_code - 搜索核心或子图给出的固定错误码。
# Returns: 可公开返回的说明文本；未知错误码使用统一兜底文案。
def _web_search_public_message(error_code: str) -> str:
    """错误码只做映射，不拼接任何内部异常、配置或工具细节。"""
    code = str(error_code or "").strip().upper()
    normalized = _WEB_SEARCH_LOCAL_CODE_ALIASES.get(code, code.lower())
    if normalized in _WEB_SEARCH_PUBLIC_MESSAGES:
        return _WEB_SEARCH_PUBLIC_MESSAGES[normalized]
    return public_error_message(code, fallback=_WEB_SEARCH_FALLBACK_MESSAGE)


# 方法作用：校验真实认证身份与状态身份一致且非匿名。
# Args: state - 当前 LangGraph 分析状态。
# Returns: 一致的 (tenant_id, user_id)；不一致、匿名或解析失败时返回 None。
def _resolve_consistent_identity(state: AnalysisState) -> tuple[int, int] | None:
    """不得从 state 或模型参数伪造身份：两边不一致即拒绝外送。"""
    from src.api.auth import get_current_tenant_id, get_current_user_id
    from src.graph.context import resolve_request_identity

    try:
        state_tenant, state_user = resolve_request_identity(state)
        current_tenant = int(get_current_tenant_id())
        current_user = int(get_current_user_id())
    except (TypeError, ValueError) as exc:
        # 解析异常正文可能包含状态中的原始取值；只保留固定脱敏文案、异常类与原堆栈。
        safe_error = RuntimeError("联网搜索身份解析失败，拒绝外送")
        logger.error(
            "联网搜索身份解析失败",
            error_type=type(exc).__name__,
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return None
    if current_user <= 0 or state_user != current_user or state_tenant != current_tenant:
        logger.warning(
            "联网搜索身份校验拒绝",
            reason="匿名或状态身份与真实认证身份不一致",
            tenant_id=current_tenant,
            user_id=current_user,
        )
        return None
    return current_tenant, current_user


# 方法作用：把检索时间统一为 UTC ISO 文本。
# Args: value - datetime 或字符串形式的检索时间。
# Returns: UTC ISO 文本；无法识别时返回原文本或空串。
def _utc_iso(value: Any) -> str:
    """时间统一用 UTC，保证引用可跨时区核验。"""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    return str(value or "").strip()


# 方法作用：把一条真实搜索来源转换为公开引用。
# Args: hit - SearchResponse 中的 SearchHit。
# Returns: 引用字典；缺少 source_id 或 URL 时返回 None。
def _hit_citation(hit: Any) -> dict[str, Any] | None:
    """引用只来自核心实际返回的来源，不接受模型自报。"""
    source_id = str(getattr(hit, "source_id", "") or "").strip()
    url = str(getattr(hit, "url", "") or "").strip()
    if not source_id or not url:
        return None
    return {
        "source_id": source_id,
        "title": str(getattr(hit, "title", "") or "").strip()[:300],
        "locator": {"url": url},
        "provider": str(getattr(hit, "provider", "") or "").strip(),
        "retrieved_at": _utc_iso(getattr(hit, "retrieved_at", "")),
        "evidence_kind": "search_snippet",
    }


# 方法作用：把一条真实来源渲染为标注不可信边界的证据文本。
# Args: hit - SearchResponse 中的 SearchHit。
# Returns: 证据上下文文本；无有效来源或内容时返回 None。
def _hit_evidence(hit: Any) -> str | None:
    """网页标题与摘要属于不可信数据，统一经 render_evidence_context 隔离。"""
    citation = _hit_citation(hit)
    if citation is None:
        return None
    content = "\n".join(
        part for part in (
            str(getattr(hit, "title", "") or "").strip(),
            str(getattr(hit, "snippet", "") or "").strip(),
        ) if part
    ).strip()
    if not content:
        return None
    from src.knowledge.asset_models import Evidence
    from src.knowledge.content_safety import render_evidence_context

    evidence = Evidence(
        content=content,
        source_id=citation["source_id"],
        version="web-search-1",
        locator=dict(citation["locator"]),
    )
    return render_evidence_context(evidence, max_chars=_MAX_EVIDENCE_PER_HIT_CHARS)


# 方法作用：把真实来源列表渲染为有界且标注不可信边界的证据上下文。
# Args: hits - SearchResponse 中的真实来源列表。
# Returns: 供模型阅读的证据文本；无有效内容时返回空串。
def _build_evidence_context(hits: list[Any]) -> str:
    """条数与总字符数双重设界，避免不可信文本占用无上限预算。"""
    blocks: list[str] = []
    used_chars = 0
    for hit in hits:
        if len(blocks) >= _MAX_EVIDENCE_HITS:
            break
        block = _hit_evidence(hit)
        if not block:
            continue
        if used_chars + len(block) > _MAX_EVIDENCE_TOTAL_CHARS:
            break
        blocks.append(block)
        used_chars += len(block)
    logger.debug("联网研究证据上下文构建完成", hit_count=len(hits), block_count=len(blocks), used_chars=used_chars)
    return "\n".join(blocks)


# 方法作用：构造只包含当前问题与不可信证据的摘要 Prompt 分段。
# Args: query - 用户当前问题；evidence_text - 有界证据上下文。
# Returns: 统一预算使用的 PromptSection 列表。
def _build_summary_sections(query: str, evidence_text: str) -> list[Any]:
    """历史、Schema、SQL、附件、技能与知识都不进入摘要路径。"""
    from src.llm.prompt_budget import PromptSection

    return [
        PromptSection("query", f"## 用户问题\n{query}", priority=100, min_chars=200, max_chars=1200),
        PromptSection(
            "evidence",
            f"## 搜索证据\n{evidence_text}",
            priority=90,
            min_chars=400,
            max_chars=_MAX_EVIDENCE_TOTAL_CHARS + 800,
        ),
    ]


# 方法作用：按真实来源生成有界的确定性来源清单摘要。
# Args: hits - 真实搜索来源；reason - 降级原因文本。
# Returns: 不依赖模型、可直接展示的来源摘要。
def _deterministic_source_summary(hits: list[Any], *, reason: str) -> str:
    """模型失效时不伪造分析，只列出可核验的来源。"""
    lines = [
        f"已检索到 {len(hits)} 条外部来源，{reason}，以下为可核验的来源清单"
        "（仅搜索摘要，未抓取网页正文）："
    ]
    for index, hit in enumerate(hits[:_MAX_FALLBACK_ITEMS], start=1):
        citation = _hit_citation(hit)
        if citation is None:
            continue
        title = (citation["title"] or citation["locator"]["url"])[:_MAX_FALLBACK_TITLE_CHARS]
        lines.append(f"{index}. 《{title}》（来源：{citation['provider'] or 'unknown'}）")
    return "\n".join(lines)[:_MAX_FALLBACK_CHARS]


# 方法作用：把模型给出的展示文本收敛为有界且非空的字符串列表。
# Args: values - 模型输出的列表；limit - 保留条数上限。
# Returns: 去空白、截断后的字符串列表。
def _bounded_texts(values: Any, *, limit: int = _MAX_MODEL_ITEMS) -> list[str]:
    """展示字段越界只截断，不让模型多写几项就丢掉整次摘要。"""
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text:
            result.append(text[:_MAX_MODEL_ITEM_CHARS])
        if len(result) >= limit:
            break
    return result


# 方法作用：通过注册 Prompt 与统一 invocation 生成有界证据摘要。
# Args: query - 用户当前问题；hits - 真实搜索来源；tenant_id/user_id - 真实认证身份。
# Returns: (结构化摘要, 降级原因)；模型不可用、失败或摘要为空时摘要为 None。
async def _summarize_hits(
    query: str,
    hits: list[Any],
    *,
    tenant_id: int,
    user_id: int,
) -> tuple[Any | None, str]:
    """不允许任何知识或探查工具：调用期显式禁用知识循环。"""
    from src.llm.client import is_task_llm_available

    if not is_task_llm_available(_SUMMARY_LLM_TASK):
        logger.warning("联网研究摘要降级", tenant_id=tenant_id, user_id=user_id, reason="模型不可用")
        return None, "摘要模型不可用"
    evidence_text = _build_evidence_context(hits)
    from src.llm.invocation import invoke_structured
    from src.llm.knowledge_loop import KnowledgeLoopConfig, use_knowledge_loop

    try:
        with use_knowledge_loop(KnowledgeLoopConfig(enabled=False)):
            parsed = await invoke_structured(
                _SUMMARY_PROMPT_ID,
                _build_summary_sections(query, evidence_text),
                task=_SUMMARY_LLM_TASK,
                temperature=0,
                metadata={"node": "market_research", "tenant_id": tenant_id, "user_id": user_id},
            )
    except asyncio.CancelledError:
        # 取消不是失败：原样传播，绝不退化成来源清单。
        raise
    except Exception as exc:
        # 模型异常正文可能包含敏感内容（如问题原文或凭证）；沿用 sql_rewriter 的
        # 安全三元组范式：固定脱敏文案 + 原始异常类 + 净化异常承载的原堆栈。
        safe_error = RuntimeError("联网研究摘要调用失败，退化为确定性来源清单")
        logger.error(
            "联网研究摘要失败",
            tenant_id=tenant_id,
            user_id=user_id,
            hit_count=len(hits),
            error_type=type(exc).__name__,
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return None, "摘要模型调用失败"
    if not str(getattr(parsed, "summary", "") or "").strip():
        logger.warning("联网研究摘要为空", tenant_id=tenant_id, user_id=user_id, reason="模型未返回摘要")
        return None, "摘要模型未返回有效内容"
    return parsed, ""


# 方法作用：构造市场研究标准非 SQL 响应增量并映射统一公开错误码。
# Args: state - 当前状态；success - 是否成功；status - 统一任务状态；summary - 公开摘要；
#       error_code - 固定公开错误码；citations - 真实引用；insights - 模型洞察；
#       limitations - 限制说明；tool_calls - 本次已发生的工具调用数。
# Returns: build_response 可直接消费的状态增量。
def _market_research_output(
    state: AnalysisState,
    *,
    success: bool,
    status: str,
    summary: str,
    error_code: str = "",
    citations: list[dict[str, Any]] | None = None,
    insights: list[str] | None = None,
    limitations: list[str] | None = None,
    tool_calls: int = 0,
) -> dict[str, Any]:
    """复用标准输出结构，但失败文案必须换成市场研究的公开错误说明。"""
    result = _mcp_standard_output(
        state,
        summary,
        success=success,
        tool_calls=tool_calls,
        citations=list(citations or []),
    )
    response = dict(result.get("final_response", {}) or {})
    response["source"] = "market_research"
    response["status"] = status
    final_summary = summary
    if error_code:
        response["error_code"] = error_code
        response["error_message"] = _web_search_public_message(error_code)
        final_summary = response["error_message"]
    analysis = dict(response.get("analysis", {}) or {})
    analysis["summary"] = final_summary
    analysis["citations"] = list(citations or [])
    analysis["insights"] = list(insights or [])
    analysis["limitations"] = list(limitations or [])
    analysis["recommended_chart_type"] = "table"
    response["analysis"] = analysis
    result["final_response"] = response
    result["analysis_result"] = analysis
    return result


# 方法作用：执行受控联网市场研究并强制引用来自真实搜索响应。
# Args: state - 当前 LangGraph 分析状态。
# Returns: 带真实来源引用、限制说明和统一状态的标准响应增量。
async def market_research_node(state: AnalysisState) -> dict:
    """门禁校验 → 确定性搜索 → 证据摘要，不使用通用 MCP Agent。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    query = contexts.request.user_query.strip()
    started = time.monotonic()
    # 入口明细只留 debug：正常执行的 INFO 预算只留给收尾那一条结果摘要。
    logger.debug(
        "市场研究子图执行开始",
        query_chars=len(query),
        tenant_id=contexts.request.tenant_id,
        user_id=contexts.request.user_id,
    )
    if not _web_search_enabled():
        logger.info("市场研究失败关闭", reason="联网搜索总开关关闭")
        return _market_research_output(
            state,
            success=False,
            status="failed",
            summary="",
            error_code="WEB_SEARCH_DISABLED",
        )
    service = _load_search_service()
    if service is None:
        logger.warning("市场研究失败关闭", reason="联网搜索核心不可用")
        return _market_research_output(
            state,
            success=False,
            status="failed",
            summary="",
            error_code="WEB_SEARCH_UNAVAILABLE",
        )
    if not query or not service.is_explicit_web_search(query):
        logger.info("市场研究等待显式联网要求", query_chars=len(query))
        return _market_research_output(
            state,
            success=True,
            status="needs_input",
            summary=_EXPLICIT_REQUIRED_MESSAGE,
            limitations=[_EXPLICIT_REQUIRED_LIMITATION],
        )
    identity = _resolve_consistent_identity(state)
    if identity is None:
        return _market_research_output(
            state,
            success=False,
            status="failed",
            summary="",
            error_code="WEB_SEARCH_IDENTITY_INVALID",
        )
    tenant_id, user_id = identity
    try:
        response = await service.search_public_web(query)
    except asyncio.CancelledError:
        # 取消原样传播：不得生成任何替代响应。
        raise
    except Exception as exc:
        # 搜索异常正文可能包含问题原文或内部细节；只保留固定脱敏文案、异常类与原堆栈。
        safe_error = RuntimeError("联网搜索调用失败，返回固定错误码")
        logger.error(
            "联网搜索调用失败",
            tenant_id=tenant_id,
            user_id=user_id,
            query_chars=len(query),
            error_type=type(exc).__name__,
            exc_info=(type(safe_error), safe_error, exc.__traceback__),
        )
        return _market_research_output(
            state,
            success=False,
            status="failed",
            summary="",
            error_code="WEB_SEARCH_FAILED",
        )
    response_success = bool(getattr(response, "success", False))
    # 固定错误码保持核心原样（大小写不做改写），仅在公开文案层做归一映射。
    response_error_code = str(getattr(response, "error_code", "") or "").strip()
    hits = list(getattr(response, "hits", []) or [])
    # 中途结果同样降为 debug，避免与外层结果摘要重复占用 INFO。
    logger.debug(
        "联网搜索返回",
        tenant_id=tenant_id,
        user_id=user_id,
        query_chars=len(query),
        success=response_success,
        hit_count=len(hits),
        error_code=response_error_code,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    if not response_success:
        # 核心的「无可用来源」码在公开层归一到统一证据缺失码。
        error_code = response_error_code or "WEB_SEARCH_FAILED"
        if error_code.lower() == "no_usable_sources":
            error_code = "MARKET_RESEARCH_EVIDENCE_MISSING"
        return _market_research_output(
            state,
            success=False,
            status="failed",
            summary="",
            error_code=error_code,
            tool_calls=1,
        )
    citations = [citation for citation in (_hit_citation(hit) for hit in hits) if citation]
    if not citations:
        logger.warning(
            "市场研究证据校验失败",
            tenant_id=tenant_id,
            user_id=user_id,
            reason="搜索结果不含可追溯来源",
        )
        return _market_research_output(
            state,
            success=False,
            status="failed",
            summary="",
            error_code="MARKET_RESEARCH_EVIDENCE_MISSING",
            tool_calls=1,
        )
    limitations = [_LIMITATION_NO_PAGE_BODY, _LIMITATION_UNTRUSTED_SOURCES]
    if bool(getattr(response, "truncated", False)):
        limitations.append(_LIMITATION_TRUNCATED)
    parsed, degraded_reason = await _summarize_hits(
        query,
        hits,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if parsed is None:
        summary = _deterministic_source_summary(hits, reason=degraded_reason)
        limitations.insert(0, f"未使用模型整理（{degraded_reason}），当前内容为系统生成的来源清单。")
        result = _market_research_output(
            state,
            success=True,
            status="partial",
            summary=summary,
            citations=citations,
            limitations=limitations,
            tool_calls=1,
        )
        logger.info(
            "市场研究子图执行完成",
            tenant_id=tenant_id,
            user_id=user_id,
            success=True,
            status="partial",
            citation_count=len(citations),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return result
    summary = str(getattr(parsed, "summary", "") or "").strip()
    limitations.extend(_bounded_texts(getattr(parsed, "limitations", []), limit=5))
    result = _market_research_output(
        state,
        success=True,
        status="success",
        summary=summary,
        citations=citations,
        insights=_bounded_texts(getattr(parsed, "insights", [])),
        limitations=limitations,
        tool_calls=1,
    )
    logger.info(
        "市场研究子图执行完成",
        tenant_id=tenant_id,
        user_id=user_id,
        success=True,
        status="success",
        citation_count=len(citations),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return result


# 方法作用：构建只包含市场研究执行节点的独立能力子图。
# Args: 无参数。
# Returns: 编译后的 LangGraph 子图。
def build_market_research_subgraph():
    """构建市场研究联网搜索与摘要子图。"""
    graph = StateGraph(AnalysisState)
    graph.add_node("research", market_research_node)
    graph.set_entry_point("research")
    graph.add_edge("research", END)
    return graph.compile()
