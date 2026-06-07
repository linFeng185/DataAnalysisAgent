"""7.1 Checkpointer — LangGraph 状态持久化 + 会话管理。

依据: SPEC §3.8.2 短期记忆
"""

from __future__ import annotations

from datetime import datetime

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.models import ConversationTurn, SessionContext

logger = get_logger(__name__)


def get_checkpointer():
    """7.1.3 Checkpointer 工厂 — 自动选择 PostgresSaver 或 MemorySaver。"""
    settings = get_settings()

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        url = settings.database_url
        if url and "postgres" in url:
            checkpointer = PostgresSaver.from_conn_string(url)
            checkpointer.setup()
            logger.info("Checkpointer 初始化", type="PostgresSaver")
            return checkpointer
    except Exception as e:
        logger.warning("PostgresSaver 不可用，降级到 MemorySaver", error=str(e))

    from langgraph.checkpoint.memory import MemorySaver
    logger.info("Checkpointer 初始化", type="MemorySaver")
    return MemorySaver()


def new_session_context(
    thread_id: str, user_id: str = "anonymous", datasource: str = "",
) -> SessionContext:
    """7.2.1 创建新的会话上下文。"""
    import uuid
    return SessionContext(
        session_id=str(uuid.uuid4())[:8],
        thread_id=thread_id,
        user_id=user_id,
        current_datasource=datasource,
    )


def record_turn(
    ctx: SessionContext,
    user_query: str,
    generated_sql: str | None = None,
    execution_success: bool = False,
    analysis_summary: str | None = None,
    chart_type: str | None = None,
) -> ConversationTurn:
    """7.2.2 记录一轮对话到会话上下文。"""
    turn = ConversationTurn(
        turn_id=len(ctx.conversation_history) + 1,
        user_query=user_query,
        generated_sql=generated_sql,
        execution_success=execution_success,
        analysis_summary=analysis_summary,
        chart_type=chart_type,
    )
    ctx.conversation_history.append(turn)
    ctx.last_active_at = datetime.now()
    ctx.last_sql = generated_sql
    ctx.last_result_summary = analysis_summary
    return turn
