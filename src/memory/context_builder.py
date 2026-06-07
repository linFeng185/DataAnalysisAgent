"""7.5 上下文裁剪 — build_llm_context 三层策略。

依据: SPEC §3.8.5 上下文窗口管理
"""

from __future__ import annotations

from src.logging_config import get_logger
from src.memory.models import ConversationTurn

logger = get_logger(__name__)

_HOT_WINDOW = 3
_WARM_WINDOW = 10
_MAX_PROMPT_TOKENS = 7000


async def build_llm_context(
    conversation_history: list[ConversationTurn],
    user_query: str = "",
    long_term_store=None,
    node_name: str = "",
) -> str:
    """7.5.1 统一上下文裁剪 — 热/温/冷三层策略。

    - 热数据: 最近 3 轮完整注入
    - 温数据: 4~10 轮压缩为摘要
    - 冷数据: 超过 10 轮走 ChromaDB 向量检索
    """
    parts: list[str] = []

    # 热: 最近 3 轮完整
    hot = conversation_history[-_HOT_WINDOW:] if conversation_history else []
    for turn in hot:
        parts.append(f"用户: {turn.user_query}")
        if turn.generated_sql:
            parts.append(f"执行的SQL: {turn.generated_sql}")
        if turn.analysis_summary:
            parts.append(f"分析结论: {turn.analysis_summary}")

    # 温: 4~10 轮摘要 (LLM 优先，规则回退)
    warm = conversation_history[-_WARM_WINDOW:-_HOT_WINDOW]
    if warm and len(conversation_history) > _HOT_WINDOW:
        parts.append(f"[前序对话摘要] {await _summarize_turns(warm)}")

    # 冷: 向量检索
    if len(conversation_history) > _WARM_WINDOW and long_term_store and user_query:
        try:
            hits = await long_term_store.search(user_query, top_k=3)
            if hits:
                qs = [h.payload.get("question", h.content[:60]) for h in hits]
                parts.append(f"[历史相关经验] {', '.join(qs)}")
        except Exception as e:
            logger.debug("长期记忆检索跳过", error=str(e))

    context = "\n---\n".join(parts)

    # 7.5.4 Token 预算检查
    if estimate_tokens(context) > _MAX_PROMPT_TOKENS:
        logger.warning("上下文超预算", estimated=estimate_tokens(context),
                       limit=_MAX_PROMPT_TOKENS, node=node_name)
        parts = []
        if hot:
            t = hot[-1]
            parts.append(f"用户: {t.user_query}")
            if t.generated_sql:
                parts.append(f"执行的SQL: {t.generated_sql}")
        if warm:
            parts.append(f"[前序对话摘要] {await _summarize_turns(warm)}")
        context = "\n---\n".join(parts)

    return context


async def _summarize_turns(turns: list[ConversationTurn]) -> str:
    """7.5.2+7.5.5 摘要生成 — LLM 优先，失败回退规则拼接。"""
    llm_summary = await _summarize_turns_llm(turns)
    if llm_summary:
        return llm_summary
    return _summarize_turns_rule(turns)


async def _summarize_turns_llm(turns: list[ConversationTurn]) -> str:
    """7.5.5 用 cheap_llm 异步预计算对话摘要。"""
    from src.llm.client import get_cheap_llm, is_llm_available
    if not is_llm_available():
        return ""

    turns_text = "\n".join(
        f"Q{i+1}: {t.user_query}"
        + (f"\nSQL: {t.generated_sql[:200]}" if t.generated_sql else "")
        + (f"\n结论: {t.analysis_summary}" if t.analysis_summary else "")
        for i, t in enumerate(turns)
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = get_cheap_llm()
        resp = await llm.ainvoke([
            SystemMessage(content="将以下多轮数据查询对话压缩为一段中文摘要（1-3句话），保留核心业务问题、数据查询目的和关键结论。"),
            HumanMessage(content=f"对话:\n{turns_text}\n\n摘要:"),
        ])
        summary = resp.content.strip() if resp.content else ""
        if summary:
            logger.info("LLM 摘要生成成功", turns=len(turns), chars=len(summary))
            return summary
        return ""
    except Exception as e:
        logger.warning("LLM 摘要生成失败，回退到规则", error=str(e))
        return ""


def _summarize_turns_rule(turns: list[ConversationTurn]) -> str:
    """7.5.2 规则拼接摘要 — 无 LLM 或 LLM 失败时的回退方案。"""
    queries = [t.user_query[:60] for t in turns if t.user_query]
    errors = sum(1 for t in turns if not t.execution_success and t.generated_sql)
    return (
        f"前 {len(turns)} 轮对话涵盖: {'; '.join(queries)}。"
        f"其中 {errors} 次查询需要重试或纠正。"
    )


def estimate_tokens(text: str) -> int:
    """7.5.4 Token 预算估算。"""
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    return int(chinese * 0.3 + (len(text) - chinese) * 0.25)
