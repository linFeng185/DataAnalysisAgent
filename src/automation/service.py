"""主动洞察与定时报告周期调度服务。"""

from __future__ import annotations

import asyncio
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


class AutomationService:
    """绑定应用生命周期并轮询 PostgreSQL 到期任务。"""

    # 方法作用：保存 Store、Runner 和轮询周期但不立即创建任务。
    # Args: self - 服务；store - 调度存储；runner - 单任务 Runner；interval_seconds - 周期。
    # Returns: 无返回值。
    def __init__(self, store: Any, runner: Any, interval_seconds: int = 60) -> None:
        self._store = store
        self._runner = runner
        self._interval_seconds = max(10, int(interval_seconds))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    # 方法作用：返回自动化后台任务是否仍在运行。
    # Args: self - 服务。
    # Returns: 后台任务存在且未结束时返回 True。
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # 方法作用：幂等启动自动化轮询任务。
    # Args: self - 服务。
    # Returns: 无返回值。
    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="scheduled-automation")
        logger.info("自动化调度服务已启动", interval_seconds=self._interval_seconds)

    # 方法作用：停止并等待后台任务退出。
    # Args: self - 服务。
    # Returns: 无返回值。
    async def close(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await self._task
        self._task = None
        logger.info("自动化调度服务已关闭")

    # 方法作用：读取并逐个执行当前到期任务，一个失败不阻断其他任务。
    # Args: self - 服务。
    # Returns: 到期、成功和失败数量摘要。
    async def run_once(self) -> dict[str, int]:
        schedules = await self._store.list_due(limit=100)
        succeeded = 0
        failed = 0
        for schedule in schedules:
            result = await self._runner.run(schedule)
            if result.get("success"):
                succeeded += 1
            else:
                failed += 1
        summary = {"due": len(schedules), "succeeded": succeeded, "failed": failed}
        logger.info("自动化调度单轮完成", **summary)
        return summary

    # 方法作用：按周期执行到期任务并在单轮异常后继续下一轮。
    # Args: self - 服务。
    # Returns: 无返回值。
    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("自动化调度单轮失败", error=str(exc), exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                continue
