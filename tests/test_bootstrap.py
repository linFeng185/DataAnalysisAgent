"""应用启动编排测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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

    # 方法作用：验证关闭阶段通过 AppContext 统一释放应用资源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_shutdown_all_closes_resources(self, monkeypatch) -> None:
        """应用关闭必须尽力释放所有共享资源。"""
        logger.debug("test_shutdown_all_closes_resources 入口")
        from src import bootstrap

        del monkeypatch
        close_context = AsyncMock()
        context = SimpleNamespace(close=close_context)

        await bootstrap.shutdown_all(context)

        close_context.assert_awaited_once()
        logger.info("test_shutdown_all_closes_resources 完成")

    # 方法作用：验证启动阶段注册可关闭的周期记忆维护服务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_start_memory_maintenance_registers_lifecycle_resource(self, monkeypatch) -> None:
        """维护服务必须由 AppContext 持有，应用关闭时才能可靠停止。"""
        # Arrange
        import src.memory.long_term_store as store_module
        import src.memory.session_archive as archive_module
        from src import app_context, bootstrap

        context = SimpleNamespace(set_resource=MagicMock())
        monkeypatch.setattr(app_context, "get_app_context", lambda: context)
        store = SimpleNamespace(pg_pool="pg-pool")
        monkeypatch.setattr(
            store_module,
            "get_long_term_memory_store",
            AsyncMock(return_value=store),
        )
        service = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        service_factory = MagicMock(return_value=service)
        monkeypatch.setattr(archive_module, "MemoryMaintenanceService", service_factory)
        settings = SimpleNamespace(
            memory_maintenance_enabled=True,
            memory_maintenance_interval_seconds=3600,
        )

        # Act
        await bootstrap._start_memory_maintenance(settings)

        # Assert
        service.start.assert_awaited_once()
        context.set_resource.assert_called_once()
        assert context.set_resource.call_args.args[:2] == ("memory_maintenance_service", service)

    # 方法作用：验证自动化调度服务按配置启动并注册 Runner 与关闭器。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_start_automation_registers_lifecycle_resources(self, monkeypatch) -> None:
        """启用自动化后后台轮询必须随 AppContext 一起关闭。"""
        # Arrange
        import src.automation.runner as runner_module
        import src.automation.service as service_module
        import src.automation.store as store_module
        from src import app_context, bootstrap

        context = SimpleNamespace(set_resource=MagicMock())
        monkeypatch.setattr(app_context, "get_app_context", lambda: context)
        store = object()
        runner = object()
        service = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        monkeypatch.setattr(store_module, "get_automation_store", lambda: store)
        monkeypatch.setattr(runner_module, "ScheduledAnalysisRunner", MagicMock(return_value=runner))
        service_factory = MagicMock(return_value=service)
        monkeypatch.setattr(service_module, "AutomationService", service_factory)
        settings = SimpleNamespace(
            automation_enabled=True,
            automation_poll_interval_seconds=45,
        )

        # Act
        await bootstrap._start_automation(settings)

        # Assert
        service_factory.assert_called_once_with(store, runner, interval_seconds=45)
        service.start.assert_awaited_once()
        assert context.set_resource.call_args_list[0].args == ("automation_runner", runner)
        assert context.set_resource.call_args_list[1].args == ("automation_service", service)
        assert context.set_resource.call_args_list[1].kwargs["closer"] is bootstrap._close_automation
