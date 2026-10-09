"""Chroma 读路径 offload 回归：确认同步 SDK 不再阻塞事件循环与工具超时。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.memory.vector_store_chroma import ChromaVectorStore


class _SlowCollection:
    """同步耗时的 Chroma 集合替身，模拟真实 SDK 的阻塞读。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.query_calls = 0
        self.get_calls = 0

    # 方法作用：模拟同步 query，占用线程而非事件循环。
    # Args: kwargs - Chroma 查询参数。
    # Returns: 空结果结构。
    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls += 1
        time.sleep(self.delay)
        return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

    # 方法作用：模拟同步 get，占用线程而非事件循环。
    # Args: kwargs - Chroma 查询参数。
    # Returns: 空结果结构。
    def get(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls += 1
        time.sleep(self.delay)
        return {"ids": [], "documents": [], "metadatas": []}

    # 方法作用：模拟同步 count。
    # Args: 无。
    # Returns: 条目数 0。
    def count(self) -> int:
        return 0


class TestChromaReadOffload:
    """覆盖 27.1.2/27.1.3 的检索期限：同步读必须让出事件循环。"""

    # 方法作用：阻塞查询期间事件循环仍能推进心跳协程。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_slow_search_releases_event_loop_for_heartbeat(self) -> None:
        """阻塞查询期间事件循环仍能推进心跳协程。"""
        # Arrange
        collection = _SlowCollection(delay=0.15)
        store = ChromaVectorStore(collection)
        beats = 0

        # 心跳协程：只要事件循环未被阻塞就会持续累加。
        # Args: 无。
        # Returns: 无。
        # 方法作用：覆盖 heartbeat 的测试场景
        # Args: 无
        # Returns: 见方法内返回
        async def heartbeat() -> None:
            nonlocal beats
            while True:
                await asyncio.sleep(0.01)
                beats += 1

        # Act
        beat_task = asyncio.create_task(heartbeat())
        await store.search("状态枚举", top_k=3)
        beat_task.cancel()

        # Assert
        assert collection.query_calls == 1
        assert beats >= 3

    # 方法作用：offload 后 asyncio.wait_for 能在期限内返回，而不是等满整个同步调用。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_deadline_is_enforced_by_wait_for(self) -> None:
        """offload 后 asyncio.wait_for 能在期限内返回，而不是等满整个同步调用。"""
        # Arrange
        collection = _SlowCollection(delay=0.15)
        store = ChromaVectorStore(collection)

        # Act
        started = time.monotonic()
        try:
            await asyncio.wait_for(store.search("状态枚举", top_k=3), timeout=0.02)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
        else:
            timed_out = False
        elapsed = time.monotonic() - started

        # Assert：期限生效，且远早于 150ms 的同步调用完成时间。
        assert timed_out is True
        assert elapsed < 0.12

    # 方法作用：metadata 过滤读同样走线程，不阻塞事件循环。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_get_by_filter_is_offloaded(self) -> None:
        """metadata 过滤读同样走线程，不阻塞事件循环。"""
        # Arrange
        collection = _SlowCollection(delay=0.15)
        store = ChromaVectorStore(collection)
        ticks = 0

        # 计数协程：验证过滤读期间事件循环仍在运行。
        # Args: 无。
        # Returns: 无。
        # 方法作用：覆盖 ticker 的测试场景
        # Args: 无
        # Returns: 见方法内返回
        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        # Act
        task = asyncio.create_task(ticker())
        entries = await store.get_by_filter({"datasource": ""}, limit=5)
        task.cancel()

        # Assert
        assert entries == []
        assert collection.get_calls == 1
        assert ticks >= 3
