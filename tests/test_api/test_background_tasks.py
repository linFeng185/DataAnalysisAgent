"""API 后台任务异常可见性回归测试。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock


logger = logging.getLogger(__name__)


class TestBackgroundTaskTracking:
    """覆盖后台任务强引用和完成回调异常记录。"""

    # 方法作用：验证后台协程异常由完成回调记录且任务最终释放。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_failed_task_is_logged_and_released(self, monkeypatch) -> None:
        """fire-and-forget 任务失败时不得由事件循环静默处理。"""
        logger.debug("test_failed_task_is_logged_and_released 入口")
        try:
            # Arrange
            import src.api.background_tasks as task_module

            captured_logger = MagicMock()
            monkeypatch.setattr(task_module, "logger", captured_logger)

            # 方法作用：模拟后台任务在执行阶段失败。
            # Args: 无。
            # Returns: 不返回结果，固定抛出 RuntimeError。
            async def fail() -> None:
                raise RuntimeError("background failed")

            # Act
            task = task_module.create_background_task(
                fail(),
                name="test-failure",
                context={"session_id": "session-1"},
            )
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

            # Assert
            captured_logger.error.assert_called_once()
            assert captured_logger.error.call_args.kwargs["exc_info"] is True
            assert task not in task_module.get_background_tasks()
            logger.info("test_failed_task_is_logged_and_released 完成")
        except Exception as exc:
            logger.error(
                "test_failed_task_is_logged_and_released 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证所有已知 API fire-and-forget 调用统一接入任务跟踪器。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_api_call_sites_use_tracked_background_tasks(self) -> None:
        """会话和知识任务不得直接调用 asyncio.create_task。"""
        logger.debug("test_api_call_sites_use_tracked_background_tasks 入口")
        try:
            # Arrange
            paths = [
                Path("src/api/streaming.py"),
                Path("src/api/routes/chat.py"),
                Path("src/api/routes/knowledge.py"),
            ]

            # Act
            sources = {path: path.read_text(encoding="utf-8") for path in paths}

            # Assert
            assert all("create_task(" not in source for source in sources.values())
            assert all("create_background_task(" in source for source in sources.values())
            logger.info("test_api_call_sites_use_tracked_background_tasks 完成")
        except Exception as exc:
            logger.error(
                "test_api_call_sites_use_tracked_background_tasks 异常: %s",
                exc,
                exc_info=True,
            )
            raise
