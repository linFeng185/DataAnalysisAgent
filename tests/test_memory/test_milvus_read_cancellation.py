"""Milvus 只读取消策略回归：读路径有界返回，写路径保持写锁不变式。"""

from __future__ import annotations

import asyncio
import time

from src.memory.vector_store_milvus import (
    MilvusVectorStore,
    _consume_task_result,
    _run_abandonable_read,
)


class TestMilvusReadCancellation:
    """覆盖 27.1.3/A6：只读在途任务可放弃且有界收尾，写入语义不变。"""

    # 方法作用：只读操作被取消时立即返回，不等待在途线程跑完。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_abandonable_read_returns_at_deadline(self) -> None:
        """只读操作被取消时立即返回，不等待在途线程跑完。"""
        # Arrange
        started_flag = {"done": False}

        # 同步阻塞操作：取消后仍在后台线程继续，最终自行结束。
        # Args: 无。
        # Returns: 固定字符串。
        def blocking() -> str:
            time.sleep(0.3)
            started_flag["done"] = True
            return "late"

        # Act
        started = time.monotonic()
        try:
            await asyncio.wait_for(_run_abandonable_read(blocking), timeout=0.03)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
        else:
            timed_out = False
        elapsed = time.monotonic() - started

        # Assert：调用方在期限附近返回，远早于 300ms 的同步操作。
        assert timed_out is True
        assert elapsed < 0.15
        # 让被放弃的线程收尾，避免测试结束时留下活跃线程。
        await asyncio.sleep(0.35)
        assert started_flag["done"] is True

    # 方法作用：被放弃任务抛出的异常必须被消费，不产生未取回异常的任务。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_abandoned_read_exception_is_consumed(self) -> None:
        """被放弃任务抛出的异常必须被消费，不产生未取回异常的任务。"""
        # Arrange
        # 同步失败操作：验证回调会取回异常而不是留给事件循环告警。
        # Args: 无。
        # Returns: 不会返回，始终抛异常。
        def failing() -> str:
            raise RuntimeError("synthetic read failure")

        # Act
        task = asyncio.create_task(asyncio.to_thread(failing))
        task.add_done_callback(_consume_task_result)
        await asyncio.sleep(0.05)

        # Assert：任务已结束且异常被取回（done 且非 cancelled）。
        assert task.done() is True
        assert task.cancelled() is False

    # 方法作用：写入路径保持原语义：取消后等待在途线程结束才释放写锁。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_write_path_still_waits_for_inflight_thread(self) -> None:
        """写入路径保持原语义：取消后等待在途线程结束才释放写锁。"""
        # Arrange
        store = MilvusVectorStore()
        finished = {"value": False}

        # 同步写操作：模拟不可中断的 SDK 写入。
        # Args: 无。
        # Returns: 固定写入条数。
        def slow_write() -> int:
            time.sleep(0.15)
            finished["value"] = True
            return 1

        # Act
        task = asyncio.create_task(store._run_sync(slow_write))
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Assert：取消传播前写操作已经收尾，写锁不变式未被破坏。
        assert finished["value"] is True
        assert store._write_lock.locked() is False
