"""7.1 Checkpointer — LangGraph 状态持久化 + 会话管理。

依据: SPEC §3.8.2 短期记忆
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from datetime import datetime

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.models import ConversationTurn, SessionContext

logger = get_logger(__name__)

# 保持 ctx 和 checkpointer 引用防 GC 关闭连接池
_pg_ctx = None
_pg_checkpointer = None
_mem_checkpointer = None


def configure_asyncio_event_loop() -> None:
    """在 Windows 上切换到 psycopg 异步兼容的 SelectorEventLoop。

    Args:
        无。

    Returns:
        无返回值。
    """
    logger.debug("配置异步事件循环入口", platform=sys.platform)
    if sys.platform != "win32":
        logger.info("非 Windows 环境保留默认事件循环", platform=sys.platform)
        return

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        proactor_policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
        current_policy = asyncio.get_event_loop_policy()
        if selector_policy and proactor_policy and isinstance(current_policy, proactor_policy):
            asyncio.set_event_loop_policy(selector_policy())
            logger.info("Windows 事件循环已切换为 Selector", previous=type(current_policy).__name__)
        else:
            logger.info("Windows 事件循环无需切换", current=type(current_policy).__name__)


async def get_checkpointer():
    """7.1.3 Checkpointer 工厂 — 自动选择 PostgresSaver 或 MemorySaver。

    连接池只创建一次，后续调用复用已有实例。
    ctx 引用必须保持在模块级防止 GC 关闭连接池。
    """
    global _pg_ctx, _pg_checkpointer, _mem_checkpointer
    settings = get_settings()

    # 已初始化则复用
    if _pg_checkpointer is not None:
        return _pg_checkpointer
    if _mem_checkpointer is not None:
        return _mem_checkpointer

    try:
        url = settings.database_url
        if url and "postgres" in url:
            import asyncpg
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from urllib.parse import urlparse, urlunparse
            # SQLAlchemy 格式 → asyncpg 格式
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            # 自动创建目标数据库（如不存在）
            parsed = urlparse(pg_url)
            db_name = parsed.path.lstrip("/")
            if db_name and db_name != "postgres":
                base_url = urlunparse(parsed._replace(path="/postgres"))
                try:
                    sys_conn = await asyncpg.connect(base_url)
                    exists = await sys_conn.fetchval(
                        "SELECT 1 FROM pg_database WHERE datname = $1", db_name)
                    if not exists:
                        await sys_conn.execute(f"CREATE DATABASE {db_name} ENCODING 'UTF8'")
                        logger.info("数据库已自动创建", database=db_name)
                    await sys_conn.close()
                except Exception:
                    pass
            # ctx 必须保持在模块级防 GC 关闭连接池
            _pg_ctx = AsyncPostgresSaver.from_conn_string(pg_url)
            _pg_checkpointer = await _pg_ctx.__aenter__()
            await _pg_checkpointer.setup()
            logger.info("Checkpointer 初始化", type="PostgresSaver")
            return _pg_checkpointer
    except Exception as e:
        logger.warning("PostgresSaver 不可用，降级到 MemorySaver", error=str(e))

    from langgraph.checkpoint.memory import MemorySaver
    _mem_checkpointer = MemorySaver()
    logger.info("Checkpointer 初始化", type="MemorySaver")
    return _mem_checkpointer


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
