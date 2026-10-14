"""依赖异常日志作用域：默认/嵌套/异常/取消/并发隔离（红→绿：先于 src/log_scopes.py）。

边界：纯内存，不触配置/网络/日志管线；只验证共享叶子模块的 ContextVar 语义。
"""

from __future__ import annotations

import asyncio

import pytest


# 作用：惰性导入共享叶子模块，未落地时按模块缺失红，不拖垮其他测试文件。
# Args: 无。
# Returns: src.log_scopes 模块。
def _scopes():
    """测试运行期再 import，避免顶层 ImportError 影响并行回归。"""
    import src.log_scopes as module

    return module


class TestDependencyErrorLoggingScope:
    """901 §12.11 共享叶子：显式 opt-in、任务隔离、finally 复位。"""

    # 作用：默认探测必须为 False（旧调用零影响）。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_default_is_off(self) -> None:
        """未进入作用域时共享探测恒 False。"""
        assert _scopes().dependency_error_logging_deferred() is False

    # 作用：进入作用域为 True，退出恢复 False。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_scope_sets_and_restores(self) -> None:
        """作用域语义与 token 复位。"""
        scopes = _scopes()
        with scopes.defer_dependency_error_logging():
            assert scopes.dependency_error_logging_deferred() is True
        assert scopes.dependency_error_logging_deferred() is False

    # 作用：嵌套作用域逐层恢复。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_nested_scopes_restore_tokens(self) -> None:
        """内层退出仍处作用域；外层退出恢复默认。"""
        scopes = _scopes()
        with scopes.defer_dependency_error_logging():
            with scopes.defer_dependency_error_logging():
                assert scopes.dependency_error_logging_deferred() is True
            assert scopes.dependency_error_logging_deferred() is True
        assert scopes.dependency_error_logging_deferred() is False

    # 作用：异常退出作用域后 token 复位。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_exception_restores_token(self) -> None:
        """作用域体异常不得残留静默状态。"""
        scopes = _scopes()
        with pytest.raises(RuntimeError):
            with scopes.defer_dependency_error_logging():
                raise RuntimeError("boom")
        assert scopes.dependency_error_logging_deferred() is False

    # 作用：取消退出作用域后 token 复位。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_cancellation_restores_token(self) -> None:
        """取消原样传播且不残留静默状态。"""
        scopes = _scopes()
        with pytest.raises(asyncio.CancelledError):
            with scopes.defer_dependency_error_logging():
                raise asyncio.CancelledError()
        assert scopes.dependency_error_logging_deferred() is False

    # 作用：并发任务（作用域外创建）不受当前任务作用域影响。
    # Args: self - 用例。
    # Returns: 无返回值。
    async def test_concurrent_task_isolated(self) -> None:
        """ContextVar 任务隔离：外部任务探测保持 False。"""
        scopes = _scopes()
        started = asyncio.Event()
        seen: dict[str, bool] = {}

        # 作用：在作用域外创建的任务里探测共享开关。
        # Args: 无。
        # Returns: 无返回值。
        async def probe() -> None:
            await started.wait()
            seen["value"] = scopes.dependency_error_logging_deferred()

        task = asyncio.create_task(probe())
        with scopes.defer_dependency_error_logging():
            started.set()
            await task
            assert scopes.dependency_error_logging_deferred() is True
        assert seen["value"] is False
