"""上传任务生命周期和容量上限回归测试。"""

from __future__ import annotations

import logging

import pytest


logger = logging.getLogger(__name__)


class TestUploadManagerRetention:
    """覆盖功能 6.7.3：任务过期回收和有界容量。"""

    # 方法作用：验证达到任务硬上限后拒绝继续创建未完成任务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_create_rejects_when_active_task_capacity_is_full(self, monkeypatch) -> None:
        """大量 pending 任务不得使进程内字典无限增长。"""
        logger.debug("test_create_rejects_when_active_task_capacity_is_full 入口")
        try:
            # Arrange
            import src.config as config_module
            from src.knowledge.upload_manager import UploadManager

            monkeypatch.setattr(
                config_module,
                "get_settings",
                lambda: type("Settings", (), {"multi_tenant": False})(),
            )
            manager = UploadManager(max_tasks=2, retention_seconds=60)
            manager.create("one.txt")
            manager.create("two.txt")

            # Act / Assert
            with pytest.raises(RuntimeError, match="任务队列已满"):
                manager.create("three.txt")
            assert len(manager._tasks) == 2  # noqa: SLF001
            logger.info("test_create_rejects_when_active_task_capacity_is_full 完成")
        except Exception as exc:
            logger.error(
                "test_create_rejects_when_active_task_capacity_is_full 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证超过保留时间的完成任务会在读取前自动清除。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_expired_finished_task_is_pruned(self, monkeypatch) -> None:
        """完成任务超过 TTL 后不得继续常驻内存。"""
        logger.debug("test_expired_finished_task_is_pruned 入口")
        try:
            # Arrange
            import src.config as config_module
            import src.knowledge.upload_manager as upload_module

            monkeypatch.setattr(
                config_module,
                "get_settings",
                lambda: type("Settings", (), {"multi_tenant": False})(),
            )
            clock = iter([100.0, 200.0])
            monkeypatch.setattr(upload_module.time, "monotonic", lambda: next(clock))
            manager = upload_module.UploadManager(max_tasks=2, retention_seconds=50)
            task = manager.create("done.txt")
            task.status = "done"
            task.finished_at = 100.0

            # Act
            result = manager.list_recent()

            # Assert
            assert result == []
            assert task.id not in manager._tasks  # noqa: SLF001
            logger.info("test_expired_finished_task_is_pruned 完成")
        except Exception as exc:
            logger.error("test_expired_finished_task_is_pruned 异常: %s", exc, exc_info=True)
            raise
