"""应用启动编排测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


logger = logging.getLogger(__name__)


class TestBootstrap:
    """覆盖功能 1.1.2：启动步骤顺序、环境降级和资源关闭。"""

    # 方法作用：验证所有启动阶段按固定顺序执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_bootstrap_all_runs_steps_in_order(self, monkeypatch) -> None:
        """正常启动必须依次完成迁移、工作流和各项预热。"""
        logger.debug("test_bootstrap_all_runs_steps_in_order 入口")
        from src import bootstrap

        calls: list[str] = []
        names = [name for name, _ in bootstrap._BOOTSTRAP_STEPS]
        for name in names:
            async def step(settings, *, _name=name):
                del settings
                calls.append(_name)
            monkeypatch.setattr(bootstrap, name, step)

        await bootstrap.bootstrap_all(SimpleNamespace(env="dev"))

        assert calls == names
        logger.info("test_bootstrap_all_runs_steps_in_order 完成")

    # 方法作用：验证非生产环境中单个启动阶段失败不会阻断后续阶段。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_bootstrap_all_dev_continues_after_failure(self, monkeypatch) -> None:
        """开发环境应记录失败并继续执行剩余步骤。"""
        logger.debug("test_bootstrap_all_dev_continues_after_failure 入口")
        from src import bootstrap

        completed = AsyncMock()
        failing = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(bootstrap, "_BOOTSTRAP_STEPS", (("failing", "失败步骤"), ("completed", "后续步骤")))
        monkeypatch.setattr(bootstrap, "failing", failing, raising=False)
        monkeypatch.setattr(bootstrap, "completed", completed, raising=False)

        await bootstrap.bootstrap_all(SimpleNamespace(env="dev"))

        completed.assert_awaited_once()
        logger.info("test_bootstrap_all_dev_continues_after_failure 完成")

    # 方法作用：验证生产环境中启动阶段失败会立即阻断启动。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_bootstrap_all_prod_raises_step_error(self, monkeypatch) -> None:
        """生产环境不能在基础设施初始化失败后带病启动。"""
        logger.debug("test_bootstrap_all_prod_raises_step_error 入口")
        from src import bootstrap

        failing = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(bootstrap, "_BOOTSTRAP_STEPS", (("failing", "失败步骤"),))
        monkeypatch.setattr(bootstrap, "failing", failing, raising=False)

        with pytest.raises(RuntimeError, match="boom"):
            await bootstrap.bootstrap_all(SimpleNamespace(env="prod"))
        logger.info("test_bootstrap_all_prod_raises_step_error 完成")

    # 方法作用：验证关闭阶段释放 MCP 与 PostgreSQL 连接池。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_shutdown_all_closes_resources(self, monkeypatch) -> None:
        """应用关闭必须尽力释放所有共享资源。"""
        logger.debug("test_shutdown_all_closes_resources 入口")
        from src import bootstrap

        close_mcp = AsyncMock()
        close_pool = AsyncMock()
        monkeypatch.setattr(bootstrap, "_close_mcp", close_mcp)
        monkeypatch.setattr(bootstrap, "_close_pg", close_pool)

        await bootstrap.shutdown_all()

        close_mcp.assert_awaited_once()
        close_pool.assert_awaited_once()
        logger.info("test_shutdown_all_closes_resources 完成")
