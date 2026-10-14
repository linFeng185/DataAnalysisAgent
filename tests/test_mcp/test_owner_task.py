"""owner task 关闭契约：AsyncExitStack 与 AnyIO cancel scope 必须同任务退出。

覆盖 spec/56 §4：进入与退出同任务、协作式关闭、超时取消、无悬挂 future、
关闭幂等，以及调用转发必须发生在 owner task 内。
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_mcp.lifecycle_fakes import FakeClientSession, FakeTransport, wait_until

logger = logging.getLogger(__name__)


# 方法作用：构造按真实 exc_info 形态渲染堆栈的日志替身，用于断言脱敏与单次计数。
# Args: errors - 收集已渲染 error 日志文本的列表。
# Returns: 与 owner 异常边界方法兼容的日志替身。
def capturing_logger(errors: list[str]) -> Any:
    # 方法作用：按 exc_info 形态渲染堆栈文本并记录本次 error 日志。
    # Args: message - 日志文案；fields - 结构化日志字段。
    # Returns: 无返回值。
    def record_error(message: str, **fields: Any) -> None:
        exception = fields.get("exc_info")
        if exception is True:
            stack = traceback.format_exc()
        elif isinstance(exception, tuple):
            stack = "".join(traceback.format_exception(*exception))
        else:
            stack = ""
        errors.append(message + stack)

    return SimpleNamespace(
        error=record_error,
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )


# 方法作用：构造使用假 transport 与会话的 owner 连接。
# Args: name - 连接名；transport - transport 替身；session - 可选固定会话替身。
# Returns: 未启动的 OwnedMCPConnection。
def make_connection(
    name: str,
    transport: FakeTransport,
    session: FakeClientSession | None = None,
) -> Any:
    from src.mcp_client.owner_task import OwnedMCPConnection

    if session is not None:
        # 方法作用：返回固定会话替身的工厂。
        # Args: _name - 连接名（忽略）；_streams - transport 流对（忽略）。
        # Returns: 预置的会话替身。
        def fixed_factory(_name: str, _streams: Any) -> FakeClientSession:
            return session

        return OwnedMCPConnection(name, transport, fixed_factory)
    return OwnedMCPConnection(
        name,
        transport,
        lambda _name, streams: FakeClientSession(*streams),
    )


class TestOwnedMCPConnection:
    """覆盖 spec/56 §4：owner task 进入/退出同任务、幂等、取消与调用转发。"""

    # 方法作用：验证从其他任务关闭时栈退出仍回到进入任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_enters_and_exits_stack_in_same_task(self) -> None:
        """跨任务 aclose 必须由 owner task 执行，而不是调用方任务。"""
        logger.debug("test_enters_and_exits_stack_in_same_task 入口")
        transport = FakeTransport()
        connection = make_connection("demo", transport)

        tools = await connection.start(timeout=5)
        assert [tool.name for tool in tools] == ["echo"]
        entered = transport.enter_task
        assert entered is not None
        assert entered is not asyncio.current_task()

        # 方法作用：在独立任务中发起关闭。
        # Args: 无。
        # Returns: 是否在预算内干净关闭。
        async def foreign_close() -> bool:
            return await connection.close(timeout=5)

        clean = await asyncio.create_task(foreign_close())

        assert clean is True
        assert transport.exit_task is entered
        assert connection.is_closed is True
        logger.info("test_enters_and_exits_stack_in_same_task 完成")

    # 方法作用：验证真实 anyio cancel scope 跨任务关闭不会抛 RuntimeError。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_anyio_cancel_scope_closed_from_foreign_task(self) -> None:
        """MCP SDK 的 anyio cancel scope 必须在 owner task 内退出。"""
        logger.debug("test_anyio_cancel_scope_closed_from_foreign_task 入口")
        transport = FakeTransport(anyio_scope=True)
        connection = make_connection("demo", transport)
        await connection.start(timeout=5)

        # 方法作用：在独立任务中发起关闭。
        # Args: 无。
        # Returns: 是否在预算内干净关闭。
        async def foreign_close() -> bool:
            return await connection.close(timeout=5)

        clean = await asyncio.create_task(foreign_close())

        assert clean is True
        assert transport.exit_task is transport.enter_task
        logger.info("test_anyio_cancel_scope_closed_from_foreign_task 完成")

    # 方法作用：验证关闭幂等，重复关闭不重复退出栈。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_close_is_idempotent(self) -> None:
        """重复关闭必须返回成功且只退出一次栈。"""
        logger.debug("test_close_is_idempotent 入口")
        transport = FakeTransport()
        connection = make_connection("demo", transport)
        await connection.start(timeout=5)

        first = await connection.close(timeout=5)
        second = await connection.close(timeout=5)

        assert first is True
        assert second is True
        assert transport.exit_count == 1
        logger.info("test_close_is_idempotent 完成")

    # 方法作用：验证关闭后调用工具与 ping 被明确拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_calls_after_close_are_rejected(self) -> None:
        """已关闭连接不得再转发任何调用。"""
        logger.debug("test_calls_after_close_are_rejected 入口")
        from src.mcp_client.owner_task import MCPConnectionClosedError

        transport = FakeTransport()
        connection = make_connection("demo", transport)
        await connection.start(timeout=5)
        await connection.close(timeout=5)

        with pytest.raises(MCPConnectionClosedError):
            await connection.call_tool("echo", {"text": "hi"})
        with pytest.raises(MCPConnectionClosedError):
            await connection.send_ping()
        logger.info("test_calls_after_close_are_rejected 完成")

    # 方法作用：验证工具调用在 owner task 内执行而非调用方任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_calls_run_inside_owner_task(self) -> None:
        """MCP SDK 的 fail_after 等 scope 进出必须落在同一个 owner task。"""
        logger.debug("test_calls_run_inside_owner_task 入口")
        session = FakeClientSession(tools=["echo"])
        transport = FakeTransport()
        connection = make_connection("demo", transport, session=session)
        await connection.start(timeout=5)

        result = await connection.call_tool("echo", {"text": "hi"})

        assert result.content[0].text == "hi"
        assert session.call_task is connection.owner_task
        assert session.call_task is not asyncio.current_task()
        await connection.close(timeout=5)
        logger.info("test_calls_run_inside_owner_task 完成")

    # 方法作用：验证会话初始化失败时栈仍在 owner task 内退出。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_startup_failure_closes_entered_stack_in_owner_task(self) -> None:
        """initialize 失败不得把半初始化资源留给调用方。"""
        logger.debug("test_startup_failure_closes_entered_stack_in_owner_task 入口")
        session = FakeClientSession(
            tools=["echo"], init_error=RuntimeError("initialize 失败"),
        )
        transport = FakeTransport()
        connection = make_connection("demo", transport, session=session)

        with pytest.raises(RuntimeError, match="initialize 失败"):
            await connection.start(timeout=5)

        assert connection.is_closed is True
        assert session.closed is True
        assert session.exit_task is session.enter_task
        assert transport.exit_task is transport.enter_task
        logger.info("test_startup_failure_closes_entered_stack_in_owner_task 完成")

    # 方法作用：验证 transport 进入失败时异常传播且连接自清理。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_transport_enter_failure_propagates_and_closes(self) -> None:
        """transport 建立失败必须原样抛出且不留下 owner task。"""
        logger.debug("test_transport_enter_failure_propagates_and_closes 入口")
        transport = FakeTransport(fail_on_enter=True)
        connection = make_connection("demo", transport)

        with pytest.raises(RuntimeError, match="transport 建立失败"):
            await connection.start(timeout=5)

        assert connection.is_closed is True
        assert connection.owner_task is not None
        assert connection.owner_task.done()
        logger.info("test_transport_enter_failure_propagates_and_closes 完成")

    # 方法作用：验证 owner 启动失败只记录一次 error，且保留原堆栈与异常类型定位。
    # Args: self - pytest 测试类实例；monkeypatch - 隔离日志替身的补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_owner_startup_error_logs_once_with_fixed_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """固定脱敏文案 + error_type + 原 traceback：位置可排障，正文不落盘。"""
        logger.debug("test_owner_startup_error_logs_once_with_fixed_text 入口")
        import src.mcp_client.owner_task as owner_module

        errors: list[str] = []
        monkeypatch.setattr(owner_module, "logger", capturing_logger(errors))
        transport = FakeTransport(fail_on_enter=True)
        connection = make_connection("demo", transport)

        with pytest.raises(RuntimeError, match="transport 建立失败"):
            await connection.start(timeout=5)

        assert len(errors) == 1, errors
        assert "MCP owner 连接异常结束" in errors[0]
        assert "RuntimeError" in errors[0]
        assert "Traceback (most recent call last)" in errors[0]
        assert "lifecycle_fakes" in errors[0]

    # 方法作用：验证 transport 异常携带敏感正文时 owner 日志保持脱敏。
    # Args: self - pytest 测试类实例；monkeypatch - 隔离日志替身的补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_owner_startup_does_not_log_sensitive_transport_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """异常消息中的连接凭证不得进入 owner 日志，原始异常仍原样向上传播。"""
        logger.debug("test_owner_startup_does_not_log_sensitive_transport_text 入口")
        import src.mcp_client.owner_task as owner_module

        secret = "SYNTHETIC-MCP-TRANSPORT-CREDENTIAL"
        errors: list[str] = []
        monkeypatch.setattr(owner_module, "logger", capturing_logger(errors))
        transport = FakeTransport(enter_error=RuntimeError(secret))
        connection = make_connection("demo", transport)

        with pytest.raises(RuntimeError) as failure:
            await connection.start(timeout=5)

        # 原始异常仍携带正文供上层诊断；只有日志被固定文案与脱敏堆栈替换。
        assert secret in str(failure.value)
        await connection.close(timeout=0.1)
        assert len(errors) == 1
        assert secret not in errors[0]
        assert "lifecycle_fakes" in errors[0]

    # 方法作用：验证 start 超时会取消卡住的 owner task。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_start_timeout_abandons_blocked_transport(self) -> None:
        """owner task 卡在建立连接时，超时必须取消并收尾。"""
        logger.debug("test_start_timeout_abandons_blocked_transport 入口")
        gate = asyncio.Event()
        transport = FakeTransport(enter_gate=gate)
        connection = make_connection("demo", transport)

        with pytest.raises(TimeoutError):
            await connection.start(timeout=0.05)

        assert connection.is_closed is True
        assert connection.owner_task is not None
        assert connection.owner_task.done()
        gate.set()
        logger.info("test_start_timeout_abandons_blocked_transport 完成")

    # 方法作用：验证 initialize 卡住时超时退出栈仍回到 owner task。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_start_timeout_exits_entered_stack_in_owner_task(self) -> None:
        """transport 已进入时取消，栈退出必须与进入同任务。"""
        logger.debug("test_start_timeout_exits_entered_stack_in_owner_task 入口")
        gate = asyncio.Event()
        session = FakeClientSession(tools=["echo"], init_gate=gate)
        transport = FakeTransport()
        connection = make_connection("demo", transport, session=session)

        with pytest.raises(TimeoutError):
            await connection.start(timeout=0.05)

        assert connection.is_closed is True
        assert session.exit_task is session.enter_task
        assert transport.exit_task is transport.enter_task
        gate.set()
        logger.info("test_start_timeout_exits_entered_stack_in_owner_task 完成")

    # 方法作用：验证单次调用超时不影响 owner task 后续调用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_call_timeout_keeps_owner_healthy(self) -> None:
        """调用方超时只放弃该次结果，协议不受影响。"""
        logger.debug("test_call_timeout_keeps_owner_healthy 入口")
        gate = asyncio.Event()
        session = FakeClientSession(tools=["echo"], call_gate=gate)
        transport = FakeTransport()
        connection = make_connection("demo", transport, session=session)
        await connection.start(timeout=5)

        with pytest.raises(TimeoutError):
            await connection.call_tool("echo", {"text": "slow"}, timeout=0.05)

        gate.set()
        result = await connection.call_tool("echo", {"text": "fast"}, timeout=5)

        assert result.content[0].text == "fast"
        await connection.close(timeout=5)
        logger.info("test_call_timeout_keeps_owner_healthy 完成")

    # 方法作用：验证关闭超时会取消 owner task 并完成收尾。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_close_timeout_cancels_owner_task(self) -> None:
        """owner task 无法在预算内响应时取消，但不留下悬挂任务。"""
        logger.debug("test_close_timeout_cancels_owner_task 入口")
        from src.mcp_client.owner_task import MCPConnectionClosedError

        gate = asyncio.Event()
        transport = FakeTransport(enter_gate=gate)
        connection = make_connection("demo", transport)
        start_task = asyncio.create_task(connection.start(timeout=30))
        await asyncio.sleep(0.05)

        clean = await connection.close(timeout=0.05)

        assert clean is False
        assert connection.is_closed is True
        assert connection.owner_task is not None
        assert connection.owner_task.done()
        gate.set()
        with pytest.raises(MCPConnectionClosedError):
            await start_task
        logger.info("test_close_timeout_cancels_owner_task 完成")

    # 方法作用：验证强制关闭时排队调用不悬挂、全部获得明确异常。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_pending_requests_fail_on_forced_close(self) -> None:
        """已出队与排队的请求 future 都必须被显式失败。"""
        logger.debug("test_pending_requests_fail_on_forced_close 入口")
        from src.mcp_client.owner_task import MCPConnectionClosedError

        gate = asyncio.Event()
        session = FakeClientSession(tools=["echo"], call_gate=gate)
        transport = FakeTransport()
        connection = make_connection("demo", transport, session=session)
        await connection.start(timeout=5)

        first = asyncio.create_task(connection.call_tool("echo", {"text": "a"}))
        second = asyncio.create_task(connection.call_tool("echo", {"text": "b"}))
        await wait_until(lambda: session.call_task is not None, timeout=2)

        clean = await connection.close(timeout=0.05)

        assert clean is False
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=1,
        )
        assert all(isinstance(item, MCPConnectionClosedError) for item in results)
        gate.set()
        logger.info("test_pending_requests_fail_on_forced_close 完成")

    # 方法作用：验证打标后沿因果链识别已记录异常，未记录异常不误判。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_mark_and_detect_across_cause_chain(self) -> None:
        """owner 记录的异常在 __cause__ 链上可被上游识别，新鲜异常不受影响。"""
        logger.debug("test_mark_and_detect_across_cause_chain 入口")
        from src.mcp_client.owner_task import is_error_logged, mark_error_logged

        origin = RuntimeError("origin")
        assert is_error_logged(origin) is False
        mark_error_logged(origin)
        assert is_error_logged(origin) is True

        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = origin
        assert is_error_logged(wrapped) is True

        assert is_error_logged(RuntimeError("fresh")) is False
        assert is_error_logged(None) is False
        logger.info("test_mark_and_detect_across_cause_chain 完成")

    # 方法作用：验证 owner 边界记录后向上传播的原始异常携带已记录标记。
    # Args: self - pytest 测试类实例；monkeypatch - 隔离日志替身的补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_owner_failure_keeps_original_exception_marked(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """打标不改异常正文：直接 owner 调用仍拿到原始异常且只记录一次。"""
        logger.debug("test_owner_failure_keeps_original_exception_marked 入口")
        import src.mcp_client.owner_task as owner_module
        from src.mcp_client.owner_task import is_error_logged

        secret = "SYNTHETIC-MCP-MARK-CREDENTIAL"
        errors: list[str] = []
        monkeypatch.setattr(owner_module, "logger", capturing_logger(errors))
        transport = FakeTransport(enter_error=RuntimeError(secret))
        connection = make_connection("demo", transport)

        with pytest.raises(RuntimeError) as failure:
            await connection.start(timeout=5)

        assert len(errors) == 1
        assert secret not in errors[0]
        assert secret in str(failure.value)
        assert is_error_logged(failure.value) is True
        logger.info("test_owner_failure_keeps_original_exception_marked 完成")

    # 方法作用：验证 close 被取消时仍带界完成 owner 收尾，且在途请求不悬挂。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_close_cancellation_still_finishes_owner_cleanup(self) -> None:
        """close 自身被取消必须先令 owner 完成清理，再向上传播取消。"""
        logger.debug("test_close_cancellation_still_finishes_owner_cleanup 入口")
        from src.mcp_client.owner_task import MCPConnectionClosedError

        gate = asyncio.Event()
        session = FakeClientSession(tools=["echo"], call_gate=gate)
        transport = FakeTransport()
        connection = make_connection("demo", transport, session=session)
        await connection.start(timeout=5)

        active = asyncio.create_task(connection.call_tool("echo", {"text": "a"}))
        queued = asyncio.create_task(connection.call_tool("echo", {"text": "b"}))
        await wait_until(lambda: session.call_task is not None, timeout=2)

        close_task = asyncio.create_task(connection.close(timeout=30))
        await asyncio.sleep(0.05)
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close_task, timeout=5)

        assert connection.owner_task is not None
        assert connection.owner_task.done() is True
        assert connection.is_closed is True
        assert session.closed is True
        assert session.exit_task is session.enter_task
        results = await asyncio.wait_for(
            asyncio.gather(active, queued, return_exceptions=True), timeout=1,
        )
        assert all(isinstance(item, MCPConnectionClosedError) for item in results)
        gate.set()
        logger.info("test_close_cancellation_still_finishes_owner_cleanup 完成")
