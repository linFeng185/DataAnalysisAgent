"""应用进程启动模式回归测试。"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


class TestMainRuntime:
    """覆盖开发热重载与调试器多进程冲突边界。"""

    # 方法作用：验证热重载只在开发环境、显式启用且无调试器时开启。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reload_policy_disables_debugger_and_production_multiprocess(self) -> None:
        """PyCharm 调试和生产启动不得再派生 Uvicorn reload 子进程。"""
        # Arrange
        from src.main import _should_enable_reload

        development = SimpleNamespace(env="dev", server_reload_enabled=True)
        production = SimpleNamespace(env="prod", server_reload_enabled=True)

        # Act / Assert
        assert _should_enable_reload(development, debugger_attached=False) is True
        assert _should_enable_reload(development, debugger_attached=True) is False
        assert _should_enable_reload(production, debugger_attached=False) is False

    # 方法作用：验证无热重载启动直接传应用和事件循环工厂，避免解析模块字符串二次导入。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_run_server_avoids_module_imports_without_reload(self, monkeypatch) -> None:
        """PyCharm 调试路径不得通过 app 或 loop 字符串再次导入 `src.main`。"""
        # Arrange
        import src.main as main_module

        settings = SimpleNamespace(env="dev", server_reload_enabled=True)
        application = object()
        uvicorn_run = MagicMock()
        monkeypatch.setattr(main_module, "get_settings", lambda: settings)
        monkeypatch.setattr(main_module, "_should_enable_reload", lambda _: False)
        monkeypatch.setattr(main_module, "create_app", lambda: application)
        monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=uvicorn_run))

        # Act
        main_module.run_server()

        # Assert
        assert uvicorn_run.call_args.args == (application,)
        assert uvicorn_run.call_args.kwargs["reload"] is False
        assert uvicorn_run.call_args.kwargs["loop"] is main_module.selector_event_loop_factory
