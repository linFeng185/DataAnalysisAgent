"""7.2.4~7.2.8 会话归档 + 轮次限制 + 7.4 记忆维护。

依据: SPEC §3.8.2 短期记忆边界 + §3.8.4 记忆衰减
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)

ARCHIVE_TIMEOUT_MINUTES = 30
# 兼容旧调用方的轮次兜底值；正常压缩由模型 Token 阈值决定。
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


# 方法作用：按当前模型上下文 Token 预算判断会话是否需要压缩。
# Args: ctx - 包含 conversation_history 的会话对象；context_window - 可选模型窗口；model_id - 可选模型标识。
# Returns: 达到动态压缩阈值返回 True，否则返回 False。
def check_turn_limit(ctx, context_window: int | None = None, model_id: str | None = None) -> bool:
    """7.2.5 根据当前模型输入 Token 阈值判断是否需要压缩。"""
    history = list(getattr(ctx, "conversation_history", []) or [])
    logger.debug("检查会话上下文预算入口", turns=len(history), context_window=context_window or 0)
    from src.memory.context_builder import estimate_history_tokens, get_context_budget

    history_tokens = estimate_history_tokens(history)
    has_token_content = any(
        any(
            getattr(item, field, None) or (item.get(field) if hasattr(item, "get") else None)
            for field in ("user_query", "generated_sql", "analysis_summary", "decision_summary")
        )
        for item in history
    )
    if has_token_content:
        budget = get_context_budget(context_window, model_id=model_id)
        result = history_tokens >= budget.trigger_tokens
        logger.info(
            "检查会话上下文预算完成",
            limit_reached=result,
            history_tokens=history_tokens,
            trigger_tokens=budget.trigger_tokens,
            model_id=budget.model_id,
        )
        return result
    # 旧 checkpoint 可能只存占位对象，无法估算 Token 时保留历史兼容行为。
    result = len(history) >= MAX_TURNS
    logger.info(
        "检查会话上下文预算完成",
        limit_reached=result,
        history_tokens=history_tokens,
        fallback="turn_count" if history else "empty",
    )
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
            from src.memory.context_builder import render_turn, truncate_text_to_tokens

            turns_text = "\n".join(
                f"Q{i+1}: {render_turn(t)}"
                for i, t in enumerate(old)
            )
            # 摘要模型只需看到受控前缀，避免历史富文本绕过主会话预算。
            turns_text = truncate_text_to_tokens(turns_text, 2_000)
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


# 方法作用：按动态 Token 阈值压缩 LangGraph 字典历史并保留最新上下文。
# Args: history - LangGraph conversation_history；compact_count - 可选摘要轮次上限；context_window - 可选模型窗口；model_id - 可选模型标识。
# Returns: 包含早期摘要和近期轮次的新列表。
async def compact_turn_history(
    history: list[dict[str, Any]],
    compact_count: int | None = None,
    *,
    context_window: int | None = None,
    model_id: str | None = None,
) -> list[dict[str, Any]]:
    """按模型 Token 阈值摘要旧轮次，并在硬保护线保留最新可用上下文。"""
    original = list(history or [])
    if not original:
        return []
    from src.memory.context_builder import estimate_history_tokens, get_context_budget

    budget = get_context_budget(context_window, model_id=model_id)
    original_tokens = estimate_history_tokens(original)
    has_token_content = any(
        any(
            getattr(item, field, None) or (item.get(field) if hasattr(item, "get") else None)
            for field in ("user_query", "generated_sql", "analysis_summary", "decision_summary")
        )
        for item in original
    )
    if has_token_content and original_tokens < budget.trigger_tokens:
        return original
    if not has_token_content and len(original) < MAX_TURNS:
        return original

    from src.memory.models import ConversationTurn

    normalized: list[ConversationTurn] = []
    for index, item in enumerate(original, start=1):
        normalized.append(ConversationTurn(
            turn_id=int(item.get("turn_id", index) or index) if isinstance(item, dict) else index,
            user_query=str(item.get("user_query", "") or "") if isinstance(item, dict) else str(getattr(item, "user_query", "") or ""),
            generated_sql=str(item.get("generated_sql", "") or "") if isinstance(item, dict) else str(getattr(item, "generated_sql", "") or ""),
            execution_success=bool(item.get("execution_success", True)) if isinstance(item, dict) else bool(getattr(item, "execution_success", True)),
            analysis_summary=str(item.get("analysis_summary", "") or "") if isinstance(item, dict) else str(getattr(item, "analysis_summary", "") or ""),
            chart_type=str(item.get("chart_type", "") or "") if isinstance(item, dict) else str(getattr(item, "chart_type", "") or ""),
        ))

    hot_turns = max(1, int(getattr(get_settings(), "context_hot_turns", 5) or 5))
    candidate_end = max(0, len(original) - hot_turns)
    if candidate_end <= 0:
        logger.warning(
            "会话达到压缩阈值但没有可摘要旧轮次",
            history_tokens=original_tokens,
            trigger_tokens=budget.trigger_tokens,
            model_id=budget.model_id,
        )
        return original

    max_count = candidate_end if compact_count is None else max(1, min(int(compact_count), candidate_end))
    selected_count = max_count
    for candidate in range(1, max_count + 1):
        remaining_tokens = estimate_history_tokens(original[candidate:])
        if remaining_tokens + budget.summary_reserve_tokens <= budget.target_tokens:
            selected_count = candidate
            break

    context = SimpleNamespace(conversation_history=normalized)
    summary = await summarize_old_turns(context, count=selected_count)
    recent = list(original[selected_count:])
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
    compressed_tokens = estimate_history_tokens(result)
    if compressed_tokens > budget.hard_limit_tokens:
        result = _trim_history_to_hard_limit(result, budget.hard_limit_tokens)
    logger.info(
        "短期记忆 Token 压缩完成",
        model_id=budget.model_id,
        original_turns=len(original),
        compacted_turns=selected_count,
        original_tokens=original_tokens,
        compressed_tokens=estimate_history_tokens(result),
        trigger_tokens=budget.trigger_tokens,
        target_tokens=budget.target_tokens,
        hard_limit_tokens=budget.hard_limit_tokens,
        remaining_turns=len(result),
    )
    return result


# 方法作用：在硬保护线内保留摘要和最新轮次，防止单轮富文本撑爆上下文。
# Args: history - 已包含摘要轮次的历史；max_tokens - 硬 Token 上限。
# Returns: 尽可能保留摘要与最新轮次的新历史。
def _trim_history_to_hard_limit(
    history: list[dict[str, Any]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    """从最新轮次向前选择，摘要轮次优先保留，极端情况下保留最新一轮。"""
    from src.memory.context_builder import estimate_history_tokens, render_turn, truncate_text_to_tokens

    if estimate_history_tokens(history) <= max_tokens:
        return history
    summary = [item for item in history if isinstance(item, dict) and item.get("is_summary")]
    recent = [item for item in history if not (isinstance(item, dict) and item.get("is_summary"))]
    if summary:
        # 摘要过大时先压缩摘要，为最新轮次留下最低空间。
        summary_item = dict(summary[0])
        summary_budget = max(1, max_tokens // 2)
        for field in ("analysis_summary", "user_query"):
            if summary_item.get(field):
                summary_item[field] = truncate_text_to_tokens(
                    str(summary_item[field]), summary_budget,
                )
        summary = [summary_item]
    selected_recent: list[dict[str, Any]] = []
    selected_tokens = estimate_history_tokens(summary)
    for item in reversed(recent):
        item_tokens = estimate_history_tokens([item])
        if selected_recent and selected_tokens + item_tokens > max_tokens:
            continue
        if not selected_recent and selected_tokens + item_tokens > max_tokens:
            latest = dict(item) if isinstance(item, dict) else {"user_query": render_turn(item)}
            remaining = max(1, max_tokens - selected_tokens - 8)
            for field, field_limit in (("generated_sql", 3000), ("analysis_summary", 3000), ("decision_summary", 1600), ("user_query", 1600)):
                if latest.get(field):
                    latest[field] = truncate_text_to_tokens(
                        str(latest[field]), max(1, min(field_limit // 4, remaining)),
                    )
            selected_recent.append(latest)
            break
        selected_recent.append(item)
        selected_tokens += item_tokens
    selected_recent.reverse()
    result = [*summary, *selected_recent]
    if estimate_history_tokens(result) > max_tokens and selected_recent:
        latest = dict(selected_recent[-1])
        remaining = max(1, max_tokens - estimate_history_tokens(summary) - 8)
        latest["user_query"] = truncate_text_to_tokens(
            str(latest.get("user_query", "")), remaining,
        )
        for field in ("generated_sql", "analysis_summary", "decision_summary"):
            latest[field] = ""
        result = [*summary, latest]
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
