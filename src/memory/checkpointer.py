"""7.1 Checkpointer — LangGraph 状态持久化 + 会话管理。

依据: SPEC §3.8.2 短期记忆
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from src.app_context import get_app_context
from src.db.utils import to_asyncpg_url
from src.logging_config import get_logger
from src.memory.models import ConversationTurn, SessionContext

logger = get_logger(__name__)

_CHECKPOINTER_RESOURCE = "checkpointer"


@dataclass(slots=True)
class _CheckpointerResource:
    """绑定 Checkpointer 与需保持存活的 PostgreSQL 异步上下文。"""

    checkpointer: Any
    postgres_context: Any | None = None


def _quote_postgres_identifier(identifier: str) -> str:
    """安全引用 PostgreSQL 数据库标识符。

    Args:
        identifier: 未引用的数据库名称。

    Returns:
        双引号包裹且内部引号已转义的标识符。
    """
    logger.debug("引用 PostgreSQL 标识符入口", identifier_length=len(identifier))
    if not identifier or "\x00" in identifier:
        logger.error("引用 PostgreSQL 标识符失败", error="标识符为空或包含 NUL")
        raise ValueError("数据库名称无效")
    result = '"' + identifier.replace('"', '""') + '"'
    logger.info("引用 PostgreSQL 标识符完成", identifier_length=len(identifier))
    return result


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

    每个 AppContext 只创建一次，PostgreSQL 上下文由资源关闭器保持和释放。
    """
    context = get_app_context()
    logger.debug("获取 Checkpointer 入口")
    resource = await context.get_or_create_async(
        _CHECKPOINTER_RESOURCE,
        partial(_create_checkpointer_resource, context.settings),
        closer=_close_checkpointer_resource,
    )
    logger.info(
        "获取 Checkpointer 完成",
        checkpointer_type=type(resource.checkpointer).__name__,
    )
    return resource.checkpointer


# 方法作用：按配置创建 PostgreSQL Checkpointer，失败时回退内存实现。
# Args: settings - 当前 AppContext 的应用配置。
# Returns: 包含 Checkpointer 与可选 PostgreSQL 上下文的资源句柄。
async def _create_checkpointer_resource(settings: Any) -> _CheckpointerResource:
    logger.debug("创建 Checkpointer 资源入口")
    postgres_context = None

    try:
        url = settings.database_url
        if url and "postgres" in url:
            import asyncpg
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from urllib.parse import urlparse, urlunparse
            pg_url = to_asyncpg_url(url)
            # 自动创建目标数据库（如不存在）
            parsed = urlparse(pg_url)
            db_name = parsed.path.lstrip("/")
            if db_name and db_name != "postgres":
                base_url = urlunparse(parsed._replace(path="/postgres"))
                sys_conn = None
                try:
                    sys_conn = await asyncpg.connect(base_url)
                    exists = await sys_conn.fetchval(
                        "SELECT 1 FROM pg_database WHERE datname = $1", db_name)
                    if not exists:
                        quoted_db_name = _quote_postgres_identifier(db_name)
                        await sys_conn.execute(
                            f"CREATE DATABASE {quoted_db_name} ENCODING 'UTF8'"
                        )
                        logger.info("数据库已自动创建", database=db_name)
                except Exception as exc:
                    logger.warning(
                        "目标数据库自动创建跳过",
                        database=db_name,
                        error=str(exc),
                        exc_info=True,
                    )
                finally:
                    if sys_conn is not None:
                        try:
                            await sys_conn.close()
                        except Exception as close_exc:
                            logger.error(
                                "系统数据库连接关闭失败",
                                database=db_name,
                                error=str(close_exc),
                                exc_info=True,
                            )
            postgres_context = AsyncPostgresSaver.from_conn_string(pg_url)
            checkpointer = await postgres_context.__aenter__()
            await checkpointer.setup()
            logger.info("Checkpointer 初始化", type="PostgresSaver")
            return _CheckpointerResource(checkpointer, postgres_context)
    except Exception as exc:
        logger.warning(
            "PostgresSaver 不可用，降级到 MemorySaver",
            error=str(exc),
            exc_info=True,
        )
        if postgres_context is not None:
            try:
                await postgres_context.__aexit__(None, None, None)
            except Exception:
                logger.error("失败的 PostgreSQL Checkpointer 上下文关闭异常", exc_info=True)

    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()
    logger.info("Checkpointer 初始化", type="MemorySaver")
    return _CheckpointerResource(checkpointer)


# 方法作用：关闭 Checkpointer 关联的 PostgreSQL 异步上下文。
# Args: resource - AppContext 持有的 Checkpointer 资源句柄。
# Returns: 无返回值。
async def _close_checkpointer_resource(resource: _CheckpointerResource) -> None:
    logger.debug(
        "关闭 Checkpointer 资源入口",
        postgres=resource.postgres_context is not None,
    )
    if resource.postgres_context is None:
        logger.info("关闭 Checkpointer 资源完成", skipped=True)
        return
    try:
        await resource.postgres_context.__aexit__(None, None, None)
    except Exception:
        logger.error("关闭 Checkpointer 资源失败", exc_info=True)
        raise
    logger.info("关闭 Checkpointer 资源完成", skipped=False)


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
