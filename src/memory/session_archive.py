"""7.2.4~7.2.8 会话归档 + 轮次限制 + 7.4 记忆维护。

依据: SPEC §3.8.2 短期记忆边界 + §3.8.4 记忆衰减
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.logging_config import get_logger

logger = get_logger(__name__)

ARCHIVE_TIMEOUT_MINUTES = 30
MAX_TURNS = 50


def check_archive_needed(ctx) -> bool:
    """7.2.4 会话超过 30 分钟未活动需归档。"""
    logger.debug("检查会话归档时间入口", last_active_at=str(ctx.last_active_at))
    result = (
        (datetime.now() - ctx.last_active_at).total_seconds()
        > ARCHIVE_TIMEOUT_MINUTES * 60
    )
    logger.info("检查会话归档时间完成", archive_needed=result)
    return result


def check_turn_limit(ctx) -> bool:
    """7.2.5 超过 50 轮限制。"""
    logger.debug("检查会话轮次限制入口", turns=len(ctx.conversation_history))
    result = len(ctx.conversation_history) >= MAX_TURNS
    logger.info("检查会话轮次限制完成", limit_reached=result)
    return result


async def summarize_old_turns(ctx, count: int = 20) -> str:
    """7.2.5+7.5.5 前 N 轮压缩 (LLM 优先，规则回退)。"""
    logger.debug("汇总旧会话轮次入口", count=count, total=len(ctx.conversation_history))
    old = ctx.conversation_history[:count]
    if not old:
        logger.info("汇总旧会话轮次完成", skipped=True)
        return ""

    from src.llm.client import get_cheap_llm, is_llm_available
    if is_llm_available():
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            turns_text = "\n".join(
                f"Q{i+1}: {t.user_query}"
                + (f"\nSQL: {t.generated_sql[:200]}" if t.generated_sql else "")
                for i, t in enumerate(old)
            )
            llm = get_cheap_llm()
            resp = await llm.ainvoke([
                SystemMessage(content="将多轮数据查询对话压缩为一段中文摘要，保留核心业务问题和关键结果。"),
                HumanMessage(content=f"对话:\n{turns_text}\n\n摘要:"),
            ])
            summary = resp.content.strip() if resp.content else ""
            if summary:
                ctx.conversation_history = ctx.conversation_history[count:]
                logger.info("LLM 会话摘要完成", removed=count, chars=len(summary))
                return summary
        except Exception as e:
            logger.error("LLM 会话摘要失败，回退规则", error=str(e), exc_info=True)

    # 回退规则
    queries = [t.user_query[:80] for t in old]
    successes = sum(1 for t in old if t.execution_success)
    summary = (
        f"早前 {len(old)} 轮: {'; '.join(queries[:5])}"
        f"{'...' if len(queries) > 5 else ''}。"
        f"成功 {successes}/{len(old)}。"
    )
    ctx.conversation_history = ctx.conversation_history[count:]
    logger.info("规则会话摘要完成", removed=count, remaining=len(ctx.conversation_history))
    return summary


async def on_session_start(
    user_id: str, user_query: str, memory_store=None,
) -> dict:
    """7.2.6 会话启动: 加载偏好 + 检索长期记忆。"""
    logger.debug("会话启动记忆加载入口", user_id=user_id)
    result: dict = {"preferences": {}, "related_memories": []}
    if not memory_store:
        logger.info("会话启动记忆加载完成", user_id=user_id, skipped=True)
        return result
    try:
        result["preferences"] = await memory_store.get_preferences(user_id)
        result["related_memories"] = await memory_store.search(user_query, top_k=5)
    except Exception as e:
        logger.error("会话启动记忆加载失败", error=str(e), exc_info=True)
    logger.info(
        "会话启动记忆加载完成",
        user_id=user_id,
        preferences=len(result["preferences"]),
        memories=len(result["related_memories"]),
    )
    return result


class SessionMaintenance:
    """7.4.1 记忆维护任务调度。"""

    def __init__(self, pg_pool=None, memory_store=None):
        logger.debug("SessionMaintenance.__init__ 入口", has_pg=pg_pool is not None)
        self._pg = pg_pool
        self._store = memory_store
        logger.info("SessionMaintenance.__init__ 完成", has_store=memory_store is not None)

    async def archive_sessions(self) -> int:
        """7.2.7 归档超过 30 天的会话。"""
        logger.debug("归档历史会话入口", has_pg=self._pg is not None)
        if not self._pg:
            logger.info("归档历史会话完成", skipped=True, count=0)
            return 0
        cutoff = datetime.now() - timedelta(days=30)
        try:
            result = await self._pg.execute(
                """INSERT INTO sessions_archive (thread_id, summary, archived_at)
                   SELECT thread_id, '会话于 ' || last_active_at::text || ' 归档', NOW()
                   FROM active_sessions WHERE last_active_at < $1""",
                cutoff,
            )
            count = int(str(result).split()[-1]) if result else 0
            logger.info("归档历史会话完成", skipped=False, count=count)
            return count
        except Exception as e:
            logger.error("会话归档失败", error=str(e), exc_info=True)
            return 0

    async def run_all(self) -> dict:
        """运行所有维护任务。"""
        logger.debug("运行全部记忆维护任务入口")
        results: dict[str, int] = {}
        if self._store:
            if hasattr(self._store, "decay_old_templates"):
                results["decayed"] = await self._store.decay_old_templates()
            if hasattr(self._store, "prune_low_confidence"):
                results["pruned"] = await self._store.prune_low_confidence()
        results["archived"] = await self.archive_sessions()
        logger.info("记忆维护完成", **results)
        return results
