"""API fire-and-forget 后台任务生命周期与异常跟踪。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from functools import partial
from typing import Any

from src.logging_config import get_logger


logger = get_logger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# 方法作用：返回当前仍在运行的 API 后台任务只读快照。
# Args: 无。
# Returns: 后台任务 frozenset 快照。
def get_background_tasks() -> frozenset[asyncio.Task[Any]]:
    """读取后台任务快照，供关闭流程和测试检查。"""
    logger.debug("后台任务快照读取入口")
    result = frozenset(_BACKGROUND_TASKS)
    logger.info("后台任务快照读取完成", task_count=len(result))
    return result


# 方法作用：处理后台任务完成状态，释放强引用并记录取消或异常。
# Args: task - 已完成任务；name - 任务名称；context - 安全日志上下文。
# Returns: 无返回值。
def _handle_task_done(
    task: asyncio.Task[Any],
    *,
    name: str,
    context: dict[str, Any],
) -> None:
    """消费任务异常，避免由事件循环报告未获取异常。"""
    logger.debug("后台任务完成回调入口", task_name=name, **context)
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        logger.info("后台任务已取消", task_name=name, **context)
        return
    try:
        exception = task.exception()
    except Exception as exc:
        logger.error(
            "后台任务状态读取失败",
            task_name=name,
            error=str(exc),
            exc_info=True,
            **context,
        )
        return
    if exception is not None:
        try:
            raise exception.with_traceback(exception.__traceback__)
        except BaseException as exc:
            logger.error(
                "后台任务执行失败",
                task_name=name,
                error=str(exc),
                exc_info=True,
                **context,
            )
        return
    logger.info("后台任务执行完成", task_name=name, **context)


# 方法作用：创建有强引用和完成回调的 API 后台任务。
# Args: coroutine - 待调度协程；name - 稳定任务名；context - 安全日志上下文。
# Returns: 已创建的 asyncio Task。
def create_background_task[T](
    coroutine: Coroutine[Any, Any, T],
    *,
    name: str,
    context: dict[str, Any] | None = None,
) -> asyncio.Task[T]:
    """创建可观测后台任务并避免任务被提前垃圾回收。"""
    safe_context = dict(context or {})
    logger.debug("后台任务创建入口", task_name=name, **safe_context)
    try:
        task = asyncio.create_task(coroutine, name=name)
    except Exception as exc:
        close = getattr(coroutine, "close", None)
        if callable(close):
            close()
        logger.error(
            "后台任务创建失败",
            task_name=name,
            error=str(exc),
            exc_info=True,
            **safe_context,
        )
        raise
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(
        partial(_handle_task_done, name=name, context=safe_context)
    )
    logger.info("后台任务创建完成", task_name=name, **safe_context)
    return task
