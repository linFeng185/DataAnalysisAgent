"""跨模块依赖异常日志作用域：显式 opt-in、ContextVar 任务隔离。

仅最外层安全边界（精确原文读取）进入；默认 False，旧调用/常规路径完全不受影响。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 默认 False：任何旧调用与常规路径的异常日志保持原状。
_DEPENDENCY_ERROR_LOGGING_DEFERRED: ContextVar[bool] = ContextVar(
    "dependency_error_logging_deferred", default=False
)


# 作用：显式 opt-in 的依赖异常日志作用域：期间资源建立/健康/重建/关闭/解析异常栈不在内层重复输出。
# Args: 无。
# Returns: 上下文管理器；退出（含异常/取消）时 finally 恢复 token。
@contextmanager
def defer_dependency_error_logging() -> Iterator[None]:
    """仅供最外层安全边界使用；不全局禁 logger、不改 loglevel/filter。"""
    token = _DEPENDENCY_ERROR_LOGGING_DEFERRED.set(True)
    try:
        yield
    finally:
        _DEPENDENCY_ERROR_LOGGING_DEFERRED.reset(token)


# 作用：读取当前任务是否处于延后作用域（供依赖层定点守卫）。
# Args: 无。
# Returns: 作用域内 True；默认 False。
def dependency_error_logging_deferred() -> bool:
    """只读探测；不读配置、不触 I/O、不做任何日志副作用。"""
    return _DEPENDENCY_ERROR_LOGGING_DEFERRED.get()
