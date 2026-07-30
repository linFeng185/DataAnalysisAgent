"""7.2.4~7.2.8 会话归档 + 轮次限制 + 7.4 记忆维护。

依据: SPEC §3.8.2 短期记忆边界 + §3.8.4 记忆衰减
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

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

    from src.llm.client import is_llm_available
    if is_llm_available():
        try:
            turns_text = "\n".join(
                f"Q{i+1}: {t.user_query}"
                + (f"\nSQL: {t.generated_sql[:200]}" if t.generated_sql else "")
                for i, t in enumerate(old)
            )
            from src.llm.invocation import invoke_text
            from src.llm.prompt_budget import PromptSection

            summary = await invoke_text(
                "context.summary",
                [
                    PromptSection(
                        "conversation",
                        f"## 对话\n{turns_text}\n\n请生成摘要。",
                        priority=100,
                        min_chars=1000,
                        max_chars=2800,
                    ),
                ],
                task="context_summary",
            )
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


# 方法作用：把达到轮次上限的字典历史压缩为一个摘要轮次和最近 30 轮。
# Args: history - LangGraph conversation_history；compact_count - 要摘要的旧轮次数。
# Returns: 包含摘要轮次和近期原始轮次的新列表。
async def compact_turn_history(
    history: list[dict[str, Any]],
    compact_count: int = 20,
) -> list[dict[str, Any]]:
    """压缩 LangGraph 使用的字典历史，避免短期记忆无限增长。"""
    if len(history) < MAX_TURNS:
        return list(history)
    from src.memory.models import ConversationTurn

    normalized: list[ConversationTurn] = []
    for index, item in enumerate(history, start=1):
        normalized.append(ConversationTurn(
            turn_id=int(item.get("turn_id", index) or index),
            user_query=str(item.get("user_query", "") or ""),
            generated_sql=str(item.get("generated_sql", "") or ""),
            execution_success=bool(item.get("execution_success", True)),
            analysis_summary=str(item.get("analysis_summary", "") or ""),
            chart_type=str(item.get("chart_type", "") or ""),
        ))
    context = SimpleNamespace(conversation_history=normalized)
    summary = await summarize_old_turns(context, count=min(compact_count, len(history)))
    recent = list(history[min(compact_count, len(history)):])
    summary_turn = {
        "turn_id": 0,
        "user_query": "[早期会话摘要]",
        "generated_sql": "",
        "execution_success": True,
        "analysis_summary": summary,
        "chart_type": "",
        "is_summary": True,
    }
    result = [summary_turn, *recent]
    logger.info(
        "短期记忆轮次压缩完成",
        original_turns=len(history),
        compacted_turns=min(compact_count, len(history)),
        remaining_turns=len(result),
    )
    return result


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
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        try:
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(self._pg, 0, 0, "super_admin") as connection:
                result = await connection.execute(
                    """WITH archived AS (
                       DELETE FROM sessions
                       WHERE last_active_at < $1
                       RETURNING session_id, title, datasource, first_query,
                                 user_id, tenant_id, created_at, last_active_at, turn_count
                   )
                   INSERT INTO sessions_archive (
                       session_id, title, datasource, first_query, user_id, tenant_id,
                       created_at, last_active_at, turn_count, summary, archived_at
                   )
                   SELECT session_id, title, datasource, first_query, user_id, tenant_id,
                          created_at, last_active_at, turn_count,
                          '会话于 ' || last_active_at::text || ' 归档', NOW()
                   FROM archived
                   ON CONFLICT (session_id) DO UPDATE SET
                       last_active_at = EXCLUDED.last_active_at,
                       turn_count = EXCLUDED.turn_count,
                       summary = EXCLUDED.summary,
                       archived_at = EXCLUDED.archived_at""",
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
            if hasattr(self._store, "prune_expired"):
                results["expired"] = await self._store.prune_expired()
            if hasattr(self._store, "decay_old_templates"):
                results["decayed"] = await self._store.decay_old_templates()
            if hasattr(self._store, "prune_low_confidence"):
                results["pruned"] = await self._store.prune_low_confidence()
            if hasattr(self._store, "reconcile_pending_sync"):
                results["reconciled"] = await self._store.reconcile_pending_sync()
        results["archived"] = await self.archive_sessions()
        logger.info("记忆维护完成", **results)
        return results


class MemoryMaintenanceService:
    """绑定应用生命周期的周期记忆维护服务。"""

    # 方法作用：初始化周期维护服务但不立即创建任务。
    # Args: self - 当前服务；maintenance - 维护任务集合；interval_seconds - 周期间隔。
    # Returns: 无返回值。
    def __init__(self, maintenance: SessionMaintenance, interval_seconds: int = 86_400) -> None:
        self._maintenance = maintenance
        self._interval_seconds = max(1, int(interval_seconds))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    # 方法作用：返回周期维护循环是否仍在运行。
    # Args: self - 当前服务。
    # Returns: 任务存在且未完成时返回 True。
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # 方法作用：幂等启动周期维护后台任务。
    # Args: self - 当前服务。
    # Returns: 无返回值。
    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="memory-maintenance",
        )
        logger.info("周期记忆维护服务已启动", interval_seconds=self._interval_seconds)

    # 方法作用：停止周期循环并等待后台任务结束。
    # Args: self - 当前服务。
    # Returns: 无返回值。
    async def close(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await self._task
        self._task = None
        logger.info("周期记忆维护服务已关闭")

    # 方法作用：立即运行一次维护，之后按配置周期重复直到关闭。
    # Args: self - 当前服务。
    # Returns: 无返回值。
    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._maintenance.run_all()
            except Exception as exc:
                logger.error("周期记忆维护执行失败", error=str(exc), exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                continue
