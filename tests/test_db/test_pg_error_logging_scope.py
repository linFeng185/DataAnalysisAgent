"""共享 PG 池异常日志边界（901 §12.11）：defer_pg_error_logging 显式 opt-in 作用域。

边界：不连库/不读真实配置；真实 get_pg_pool/_create_pg_pool/_close_pg_pool_resource/
pg_connection 走源码，仅 Fake 最小 AppContext 资源容器与最底 asyncpg.create_pool。
覆盖：默认不变、作用域内建立/健康/重建/关闭异常栈不重复输出、嵌套与异常/取消复位、
并发任务隔离、真实 pg_connection 建连前与 body 失败链。红→绿：先于 pg_pool.py。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

import src.memory.pg_pool as pg_pool_module

_SENTINEL = "SYNTHETIC-POOL-BODY-NOT-REAL-73ad"
_URL = "postgresql://sandbox-NOT-REAL@127.0.0.1:1/demo"


# 作用：惰性导入共享叶子模块，未落地时按模块缺失红。
# Args: 无。
# Returns: src.log_scopes 模块。
def _shared_scopes():
    """共享作用域模块由本文件按需读取，不落顶层 import。"""
    import src.log_scopes as module

    return module


# 作用：过滤 error/warning 级事件，供“内层零输出/默认照记”断言。
# Args: logs - capture_logs 事件列表。
# Returns: 高阶事件子列表。
def _error_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留 error/warning，避免 debug/info 噪声干扰判定。"""
    return [
        event for event in logs if str(event.get("log_level", "")).lower() in {"error", "warning"}
    ]


# 作用：最小 AppContext 资源容器替身：只做资源持有/创建/关闭。
# Args: pool - 初始资源（可为 None）。
# Returns: 无返回值（实例化）。
class _FakeContext:
    """不触真实配置与生命周期，仅承载工厂调用与关闭计数。"""

    # 作用：绑定初始资源与设置替身。
    # Args: pool - 既有池或 None。
    # Returns: 无返回值。
    def __init__(self, *, pool: Any = None) -> None:
        self.settings = SimpleNamespace(database_url=_URL)
        self._resource = pool
        self.close_calls = 0

    # 作用：读取资源引用。
    # Args: name - 资源名。
    # Returns: 资源或 None。
    def get_resource(self, name: str) -> Any:
        """只查内存引用，不做健康检查。"""
        return self._resource

    # 作用：按需调用工厂创建资源（真实 _create_pg_pool 由工厂回调触发）。
    # Args: name - 资源名；factory - 创建协程；closer - 关闭器。
    # Returns: 资源对象。
    async def get_or_create_async(self, name: str, factory: Any, *, closer: Any = None) -> Any:
        """失败时保持 None，供重建路径再次触发。"""
        if self._resource is None:
            self._resource = await factory()
        return self._resource

    # 作用：关闭资源并计数。
    # Args: name - 资源名。
    # Returns: 是否曾持有资源。
    async def close_resource(self, name: str) -> bool:
        """健康重建前调用，语义与真实容器一致。"""
        self.close_calls += 1
        existed = self._resource is not None
        self._resource = None
        return existed


# 作用：健康检查失败替身：acquire() 直接抛错。
# Args: 无。
# Returns: 无返回值（实例化）。
class _BrokenPool:
    """模拟池失效，触发 get_pg_pool 的 warning 重建分支。"""

    # 作用：抛出健康检查异常。
    # Args: self - 池替身。
    # Returns: 不返回；始终抛 RuntimeError。
    def acquire(self) -> Any:
        """正文哨兵仅用于默认路径日志断言。"""
        raise RuntimeError(f"health body={_SENTINEL}")


# 作用：关闭失败替身：close() 抛错。
# Args: 无。
# Returns: 无返回值（实例化）。
class _CloseBrokenPool:
    """触发 _close_pg_pool_resource 的 error 分支。"""

    # 作用：抛出关闭异常。
    # Args: self - 池替身。
    # Returns: 不返回；始终抛 RuntimeError。
    async def close(self) -> None:
        """正文哨兵仅用于默认路径日志断言。"""
        raise RuntimeError(f"close body={_SENTINEL}")


# 作用：空事务上下文替身（身份设置 SQL 用真实 pg_pool_connection 执行）。
# Args: 无。
# Returns: 无返回值（实例化）。
class _NullTx:
    """enter/exit 均不做事。"""

    # 作用：进入事务。
    # Args: self - 事务替身。
    # Returns: None。
    async def __aenter__(self) -> None:
        """不开启真实事务。"""

    # 作用：退出事务。
    # Args: self - 事务替身；exc - 异常三元组。
    # Returns: None。
    async def __aexit__(self, *exc: object) -> None:
        """不吞异常，原样放行。"""


# 作用：可 acquire 的连接替身（fetchval/execute 记录）。
# Args: 无。
# Returns: 无返回值（实例化）。
class _FakeConnection:
    """健康检查与身份设置都走真实 SQL 调用点。"""

    # 作用：初始化查询替身。
    # Args: self - 连接替身。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.execute = AsyncMock(return_value="OK")
        self.fetchval = AsyncMock(return_value=1)

    # 作用：返回事务上下文。
    # Args: self - 连接替身。
    # Returns: 事务上下文替身。
    def transaction(self) -> _NullTx:
        """只提供 async CM 协议。"""
        return _NullTx()


# 作用：acquire 上下文替身。
# Args: connection - 出借连接。
# Returns: 无返回值（实例化）。
class _AcquireCM:
    """enter 出借连接，exit 原样放行。"""

    # 作用：绑定连接。
    # Args: connection - 连接替身。
    # Returns: 无返回值。
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    # 作用：进入 acquire 上下文。
    # Args: self - 上下文。
    # Returns: 连接替身。
    async def __aenter__(self) -> _FakeConnection:
        """不建立真实连接。"""
        return self._connection

    # 作用：退出 acquire 上下文。
    # Args: self - 上下文；exc - 异常三元组。
    # Returns: None。
    async def __aexit__(self, *exc: object) -> None:
        """原样放行异常。"""


# 作用：健康检查可通过的池替身。
# Args: 无。
# Returns: 无返回值（实例化）。
class _FakePool:
    """供真实 pg_connection 的身份设置与 body 失败链使用。"""

    # 作用：初始化连接与关闭计数。
    # Args: self - 池替身。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.connection = _FakeConnection()
        self.closed = 0

    # 作用：返回 acquire 上下文。
    # Args: self - 池替身。
    # Returns: acquire 上下文替身。
    def acquire(self) -> _AcquireCM:
        """每次出借同一内存连接。"""
        return _AcquireCM(self.connection)

    # 作用：模拟关闭。
    # Args: self - 池替身。
    # Returns: None。
    async def close(self) -> None:
        """真实关闭由 closer 计数承接。"""
        self.closed += 1


# 作用：装配 Fake 容器与最底 create_pool 注入。
# Args: monkeypatch - 补丁器；pool - 既有池；create_error - 是否注入 create_pool 抛错。
# Returns: Fake 容器。
def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pool: Any = None,
    create_error: bool = True,
) -> _FakeContext:
    """真实 get_pg_pool/_create_pg_pool 走源码，仅替换容器与 asyncpg.create_pool。"""
    context = _FakeContext(pool=pool)
    monkeypatch.setattr(pg_pool_module, "get_app_context", lambda: context)
    if create_error:
        # 作用：模拟 asyncpg.create_pool 抛错（最底层注入点）。
        # Args: args/kwargs - 透传参数（真实调用首参为连接串，位置传参）。
        # Returns: 不返回；始终抛 RuntimeError。
        async def _failing_create_pool(*args: object, **kwargs: object) -> Any:
            """正文哨兵只应出现在默认路径的既有日志里。"""
            raise RuntimeError(f"create body={_SENTINEL}")

        monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _failing_create_pool)
    return context


class TestDeferScopeDefaults:
    """J 组：默认旧行为与作用域静默、token 复位、并发隔离。"""

    # 作用：默认无作用域时真实建立链照旧记录两处异常栈。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_default_real_chain_logs_inner_stacks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实 get_pg_pool→_create_pg_pool 链：创建与获取各记一次 error。"""
        _install(monkeypatch)
        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                await pg_pool_module.get_pg_pool()
        events = [event["event"] for event in _error_events(logs)]
        assert "创建 PG 池失败" in events
        assert "get_pg_pool 失败" in events

    # 作用：作用域内同一真实链零高阶日志、异常照常传播。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_scope_suppresses_inner_chain_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内层不重复输出异常栈，交由最外层安全边界一次记录。"""
        _install(monkeypatch)
        with capture_logs() as logs:
            with pg_pool_module.defer_pg_error_logging():
                with pytest.raises(RuntimeError):
                    await pg_pool_module.get_pg_pool()
        assert _error_events(logs) == []

    # 作用：嵌套作用域 token 正确复位且不重复计数。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_nested_scope_tokens_restore(self) -> None:
        """内层退出后仍处作用域；外层退出后恢复默认。"""
        scopes = _shared_scopes()
        assert scopes.dependency_error_logging_deferred() is False
        with pg_pool_module.defer_pg_error_logging():
            assert scopes.dependency_error_logging_deferred() is True
            with pg_pool_module.defer_pg_error_logging():
                assert scopes.dependency_error_logging_deferred() is True
            assert scopes.dependency_error_logging_deferred() is True
        assert scopes.dependency_error_logging_deferred() is False

    # 作用：异常退出作用域后 token 复位。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_scope_resets_after_exception(self) -> None:
        """作用域体异常不得残留静默状态。"""
        with pytest.raises(RuntimeError):
            with pg_pool_module.defer_pg_error_logging():
                raise RuntimeError("boom")
        assert _shared_scopes().dependency_error_logging_deferred() is False

    # 作用：取消退出作用域后 token 复位。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_scope_resets_after_cancellation(self) -> None:
        """取消原样传播且不残留静默状态。"""
        with pytest.raises(asyncio.CancelledError):
            with pg_pool_module.defer_pg_error_logging():
                raise asyncio.CancelledError()
        assert _shared_scopes().dependency_error_logging_deferred() is False


class TestDependencyLayerDefaults:
    """M 组：AppContext 异步创建/单资源关闭与 db.utils 解析：默认照记、作用域静默。"""

    # 作用：真实 AppContext 工厂失败默认保留原 error 事件与字段。
    # Args: self - 用例。
    # Returns: 无返回值。
    async def test_app_context_factory_default_then_deferred(self) -> None:
        """默认 `error=str(exc)` 字段逐字保留；作用域内零高阶事件、异常照抛。"""
        from src.app_context import AppContext

        context = AppContext(settings=SimpleNamespace(multi_tenant=False))
        note = f"factory body={_SENTINEL}"

        # 作用：始终抛错的异步工厂。
        # Args: 无。
        # Returns: 不返回；始终抛 RuntimeError。
        async def failing_factory() -> object:
            raise RuntimeError(note)

        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                await context.get_or_create_async("pg_pool", failing_factory)
        default_events = [event for event in _error_events(logs)]
        assert [event["event"] for event in default_events] == ["异步获取 AppContext 资源失败"]
        assert default_events[0].get("error") == note
        assert default_events[0].get("resource") == "pg_pool"

        with capture_logs() as logs:
            with _shared_scopes().defer_dependency_error_logging():
                with pytest.raises(RuntimeError):
                    await context.get_or_create_async("pg_pool", failing_factory)
        assert _error_events(logs) == []

    # 作用：真实 close_resource 清理器失败默认照记、作用域静默。
    # Args: self - 用例。
    # Returns: 无返回值。
    async def test_close_resource_default_then_deferred(self) -> None:
        """单资源关闭失败链：默认字段不变；作用域内零高阶事件。"""
        from src.app_context import AppContext

        note = f"closer body={_SENTINEL}"

        # 作用：始终抛错的同步关闭器。
        # Args: value - 资源值。
        # Returns: 不返回；始终抛 RuntimeError。
        def failing_closer(value: object) -> None:
            raise RuntimeError(note)

        # 作用：构造带失败关闭器资源的真实 AppContext 实例。
        # Args: 无。
        # Returns: 每次调用新建的 AppContext。
        def _context() -> AppContext:
            ctx = AppContext(settings=SimpleNamespace(multi_tenant=False))
            ctx.set_resource("pg_pool", object(), closer=failing_closer)
            return ctx

        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                await _context().close_resource("pg_pool")
        default_events = [event for event in _error_events(logs)]
        assert [event["event"] for event in default_events] == ["关闭 AppContext 资源失败"]
        assert default_events[0].get("error") == note

        with capture_logs() as logs:
            with _shared_scopes().defer_dependency_error_logging():
                with pytest.raises(RuntimeError):
                    await _context().close_resource("pg_pool")
        assert _error_events(logs) == []

    # 作用：真实 DSN parser 失败默认照记、作用域静默。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_db_parser_default_then_deferred(self) -> None:
        """to_asyncpg_url 非法 DSN：默认 error 事件保留；作用域内零高阶事件。"""
        from src.db.utils import to_asyncpg_url

        with capture_logs() as logs:
            with pytest.raises(ValueError):
                to_asyncpg_url("mysql://sandbox-NOT-REAL/demo")
        assert [event["event"] for event in _error_events(logs)] == ["to_asyncpg_url 失败"]

        with capture_logs() as logs:
            with _shared_scopes().defer_dependency_error_logging():
                with pytest.raises(ValueError):
                    to_asyncpg_url("mysql://sandbox-NOT-REAL/demo")
        assert _error_events(logs) == []

    # 作用：并发任务（作用域外创建）不被静默。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_concurrent_task_not_silenced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """作用域内调用被静默时，另一任务的同一建立链仍记两次 error。"""
        _install(monkeypatch)
        calls = {"count": 0}
        first = asyncio.Event()
        legacy_done = asyncio.Event()

        # 作用：首个调用等待并发任务完成后再抛错。
        # Args: args/kwargs - 透传参数。
        # Returns: 不返回；始终抛 RuntimeError。
        async def _gated_create_pool(*args: object, **kwargs: object) -> Any:
            """用事件门精确安排“作用域内 vs 并发旧调用”的时序。"""
            calls["count"] += 1
            if calls["count"] == 1:
                first.set()
                await legacy_done.wait()
            raise RuntimeError(f"create body={_SENTINEL}")

        monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _gated_create_pool)

        # 作用：作用域外创建的旧调用任务。
        # Args: 无。
        # Returns: 无返回值。
        async def legacy_call() -> None:
            await first.wait()
            with pytest.raises(RuntimeError):
                await pg_pool_module.get_pg_pool()
            legacy_done.set()

        task = asyncio.create_task(legacy_call())
        with capture_logs() as logs:
            with pg_pool_module.defer_pg_error_logging():
                with pytest.raises(RuntimeError):
                    await pg_pool_module.get_pg_pool()
            await task
        events = [event["event"] for event in _error_events(logs)]
        assert events.count("创建 PG 池失败") == 1
        assert events.count("get_pg_pool 失败") == 1


class TestHealthRebuildAndClose:
    """K 组：健康检查失败重建与关闭失败在作用域内的静默。"""

    # 作用：健康检查失败重建链默认记三处，作用域内零输出。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_health_rebuild_failure_logs_deferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """warning 重建 + 创建失败 + 获取失败三处栈均受作用域覆盖。"""
        _install(monkeypatch, pool=_BrokenPool())
        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                await pg_pool_module.get_pg_pool()
        default_events = [event["event"] for event in _error_events(logs)]
        assert "PG 池失效，重建" in default_events
        assert "创建 PG 池失败" in default_events
        assert "get_pg_pool 失败" in default_events

        _install(monkeypatch, pool=_BrokenPool())
        with capture_logs() as logs:
            with pg_pool_module.defer_pg_error_logging():
                with pytest.raises(RuntimeError):
                    await pg_pool_module.get_pg_pool()
        assert _error_events(logs) == []

    # 作用：关闭资源失败默认记一次，作用域内零输出。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_close_resource_failure_deferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_close_pg_pool_resource 的 error 分支同样受作用域覆盖。"""
        pool = _CloseBrokenPool()
        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                await pg_pool_module._close_pg_pool_resource(pool)
        assert [event["event"] for event in _error_events(logs)] == ["关闭 PG 池资源失败"]

        with capture_logs() as logs:
            with pg_pool_module.defer_pg_error_logging():
                with pytest.raises(RuntimeError):
                    await pg_pool_module._close_pg_pool_resource(pool)
        assert _error_events(logs) == []


class TestRealPgConnectionChain:
    """L 组：真实 pg_connection 包 Fake 池的建立前与 body 失败链。"""

    # 作用：建连前建立失败：默认记入创建与 pg_connection，作用域内零输出。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_pre_establish_failure_deferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """真实 pg_connection 下的建立失败不得越过最外层安全边界。"""
        _install(monkeypatch)
        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                async with pg_pool_module.pg_connection(7, 9, "analyst"):
                    pass
        default_events = [event["event"] for event in _error_events(logs)]
        assert "创建 PG 池失败" in default_events
        assert "pg_connection 失败" in default_events

        _install(monkeypatch)
        with capture_logs() as logs:
            with pg_pool_module.defer_pg_error_logging():
                with pytest.raises(RuntimeError):
                    async with pg_pool_module.pg_connection(7, 9, "analyst"):
                        pass
        assert _error_events(logs) == []

    # 作用：body 失败：默认记一次 pg_connection，作用域内零输出。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_body_failure_deferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """身份设置成功后的业务异常只受最外层安全记录。"""
        _install(monkeypatch, pool=_FakePool(), create_error=False)
        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                async with pg_pool_module.pg_connection(7, 9, "analyst"):
                    raise RuntimeError(f"body={_SENTINEL}")
        assert [event["event"] for event in _error_events(logs)] == ["pg_connection 失败"]

        _install(monkeypatch, pool=_FakePool(), create_error=False)
        with capture_logs() as logs:
            with pg_pool_module.defer_pg_error_logging():
                with pytest.raises(RuntimeError):
                    async with pg_pool_module.pg_connection(7, 9, "analyst"):
                        raise RuntimeError(f"body={_SENTINEL}")
        assert _error_events(logs) == []
