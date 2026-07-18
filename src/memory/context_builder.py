"""7.5 上下文裁剪 — build_llm_context 三层策略。

依据: SPEC §3.8.5 上下文窗口管理
"""

from __future__ import annotations

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.models import ConversationTurn

logger = get_logger(__name__)


def _g(turn, key: str):
    """兼容 ConversationTurn 对象和 checkpointer 反序列化的 dict。"""
    if hasattr(turn, 'get'):
        return turn.get(key, '') or ''
    return getattr(turn, key, '') or ''


async def build_llm_context(
    conversation_history: list[ConversationTurn],
    user_query: str = "",
    long_term_store=None,
    node_name: str = "",
) -> str:
    """7.5.1 统一上下文裁剪 — 热/温/冷三层策略。

    - 热数据: 最近 N 轮完整注入 (context_hot_turns)
    - 温数据: N+1 ~ M 轮压缩为摘要 (context_warm_turns)
    - 冷数据: 超过 M 轮走 ChromaDB 向量检索

    参数通过 settings 配置，可按模型上下文窗口调整。
    """
    s = get_settings()
    hot_window = s.context_hot_turns
    warm_window = s.context_warm_turns
    max_tokens = s.context_max_tokens
    parts: list[str] = []

    # 热: 最近 3 轮完整
    hot = conversation_history[-hot_window:] if conversation_history else []
    for turn in hot:
        parts.append(f"用户: {_g(turn, 'user_query')}")
        if _g(turn, 'generated_sql'):
            parts.append(f"执行的SQL: {_g(turn, 'generated_sql')}")
        if _g(turn, 'analysis_summary'):
            parts.append(f"分析结论: {_g(turn, 'analysis_summary')}")

    # 温: 4~10 轮摘要 (LLM 优先，规则回退)
    warm = conversation_history[-warm_window:-hot_window]
    if warm and len(conversation_history) > hot_window:
        parts.append(f"[前序对话摘要] {await _summarize_turns(warm)}")

    # 冷: 向量检索
    if len(conversation_history) > warm_window and long_term_store and user_query:
        try:
            hits = await long_term_store.search(user_query, top_k=3)
            if hits:
                qs = [h.payload.get("question", h.content[:60]) for h in hits]
                parts.append(f"[历史相关经验] {', '.join(qs)}")
        except Exception as e:
            logger.debug("长期记忆检索跳过", error=str(e))

    context = "\n---\n".join(parts)

    # 7.5.4 Token 预算检查
    if estimate_tokens(context) > max_tokens:
        logger.warning("上下文超预算", estimated=estimate_tokens(context),
                       limit=max_tokens, node=node_name)
        parts = []
        if hot:
            t = hot[-1]
            parts.append(f"用户: {_g(t, 'user_query')}")
            if _g(t, 'generated_sql'):
                parts.append(f"执行的SQL: {_g(t, 'generated_sql')}")
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
    """用 LLM 压缩对话摘要。

    使用原生 HTTP 调用而非 LangChain LLM 接口，
    确保摘要请求不会被 LangGraph astream_events 捕获并流式输出到前端。
    """
    from src.config import get_settings
    from src.llm.client import is_task_llm_available, resolve_llm_task_target
    if not is_task_llm_available("context_summary"):
        return ""

    turns_text = "\n".join(
        f"Q{i+1}: {_g(t, 'user_query')}"
        + (f"\nSQL: {_g(t, 'generated_sql')[:200]}" if _g(t, 'generated_sql') else "")
        + (f"\n结论: {_g(t, 'analysis_summary')}" if _g(t, 'analysis_summary') else "")
        for i, t in enumerate(turns)
    )

    try:
        import aiohttp
        s = get_settings()
        target = resolve_llm_task_target("context_summary", settings=s)
        if target == "local":
            summary_model = s.context_summary_model or s.local_llm_model
            base_url = s.local_llm_base_url
            api_key = s.local_llm_api_key or "local"
            timeout_seconds = s.local_llm_timeout
        else:
            summary_model = s.context_summary_model or s.cheap_llm_model or s.llm_model
            base_url = s.openai_base_url
            api_key = s.openai_api_key
            timeout_seconds = min(s.llm_timeout, 15)
        payload = {
            "model": summary_model,
            "messages": [
                {"role": "system", "content": "将以下多轮数据查询对话压缩为一段中文摘要（1-3句话），保留核心业务问题、数据查询目的和关键结论。"},
                {"role": "user", "content": f"对话:\n{turns_text}\n\n摘要:"},
            ],
            "temperature": 0,
            "max_tokens": 200,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    summary = data["choices"][0]["message"]["content"].strip()
                    if summary:
                        logger.info("LLM 摘要生成成功", turns=len(turns), chars=len(summary))
                        return summary
                else:
                    logger.debug("LLM 摘要请求失败", status=resp.status)
        return ""
    except Exception as e:
        logger.warning("LLM 摘要生成失败，回退到规则", error=str(e))
        return ""


def _summarize_turns_rule(turns: list[ConversationTurn]) -> str:
    """7.5.2 规则拼接摘要 — 无 LLM 或 LLM 失败时的回退方案。"""
    queries = [_g(t, 'user_query')[:60] for t in turns if _g(t, 'user_query')]
    errors = sum(1 for t in turns if not _g(t, 'execution_success') and _g(t, 'generated_sql'))
    return (
        f"前 {len(turns)} 轮对话涵盖: {'; '.join(queries)}。"
        f"其中 {errors} 次查询需要重试或纠正。"
    )


def estimate_tokens(text: str) -> int:
    """7.5.4 Token 预算估算。"""
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    return int(chinese * 0.3 + (len(text) - chinese) * 0.25)
