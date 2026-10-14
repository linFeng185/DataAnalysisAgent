"""owner task 通道：MCP 连接的进入、调用与退出必须落在同一条任务内。

依据: spec/56 §4。stdio/sse transport 与 ClientSession 的 AsyncExitStack 内
含 AnyIO cancel scope，跨任务 aclose 会抛
"Attempted to exit cancel scope in a different task than it was entered in"，
造成旧连接无法关闭、取消传播污染无关任务。本模块让每条连接由专属 owner
task 持有：其他任务只能通过队列请求调用或协作式关闭。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)

# 强制取消 owner task 后，允许其完成栈退出的宽限时长（秒）。
_CANCEL_GRACE_SECONDS = 5.0

# 默认关闭预算（秒），保证关闭有界。
_DEFAULT_CLOSE_TIMEOUT = 10.0

# owner task 请求队列的操作类型。
_OP_CALL = "call"
_OP_PING = "ping"
_OP_SHUTDOWN = "shutdown"

# 跨层单次记录标记：owner 处理边界已为同一异常落盘脱敏堆栈，上游不得重复记录。
_MCP_ERROR_LOGGED_ATTR = "_mcp_error_logged"


# 方法作用：把异常替换成脱敏异常并保留原始堆栈，供日志安全记录异常位置。
# Args: exc - 待记录的原始异常；message - 固定脱敏异常文案。
# Returns: (type, value, traceback) 三元组，可直接作为 exc_info 传给 logger。
def _safe_exc_info(exc: BaseException, message: str) -> tuple[type, BaseException, Any]:
    # transport 异常正文可能携带 URL 与凭证，不能让原文进入日志。
    safe_error = RuntimeError(message)
    return (type(safe_error), safe_error, exc.__traceback__)


# 方法作用：在 owner 边界完成脱敏记录后给异常打标，声明同一异常已落盘。
# Args: exc - owner 边界刚刚记录过的原始异常对象。
# Returns: 无返回值；异常对象不接受自定义属性时静默跳过，不影响异常继续传播。
def mark_error_logged(exc: BaseException) -> None:
    """标记异常已在处理边界记录，供上游判断避免重复落盘同一堆栈。

    Args:
        exc: owner 处理边界刚刚完成脱敏记录的异常。

    Returns:
        无返回值；打标失败只影响跨层去重，不影响异常传播。
    """
    # 个别异常对象禁止写属性：放弃打标只退化为上游可能重复记录一次，不影响传播。
    with suppress(Exception):
        setattr(exc, _MCP_ERROR_LOGGED_ATTR, True)


# 方法作用：沿 __cause__/__context__ 链判断异常是否已在处理边界记录过。
# Args: exc - 待判断的异常对象，可为 None。
# Returns: 因果链上任一异常带已记录标记时返回 True。
def is_error_logged(exc: BaseException | None) -> bool:
    """判断异常及其因果链是否已由处理边界完成脱敏记录。

    Args:
        exc: 待判断的异常对象，可为 None。

    Returns:
        链上任一异常已记录时返回 True；否则返回 False。
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, _MCP_ERROR_LOGGED_ATTR, False):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class MCPConnectionClosedError(RuntimeError):
    """连接已关闭或未启动成功时仍尝试调用与等待。"""


class OwnedMCPConnection:
    """由专属 owner task 持有的 MCP 连接生命周期。

    契约（spec/56 §4）：

    - `start()` 在 owner task 内进入 transport 与 session 并返回工具描述；
      失败或超时由 owner task 自行退出栈，不留下半初始化对象。
    - `call_tool()` / `send_ping()` 通过请求队列转发，保证 SDK 内部
      `fail_after` 等 scope 的进入与退出同任务。
    - `close()` 协作式投递 shutdown，超时后取消 owner task（取消仍在该任务
      内触发栈退出），等待有界、幂等；未完成的请求 future 全部显式失败。
    """

    # 方法作用：初始化 owner 连接的状态与请求队列。
    # Args: name - 连接名；transport - MCP transport 异步上下文；
    #       session_factory - 接收 (name, streams) 返回会话异步上下文的工厂。
    # Returns: 无返回值。
    def __init__(
        self,
        name: str,
        transport: Any,
        session_factory: Callable[[str, Any], Any],
    ) -> None:
        self.name = name
        self._transport = transport
        self._session_factory = session_factory
        self._queue: asyncio.Queue[
            tuple[str, Any, asyncio.Future[Any] | None]
        ] = asyncio.Queue()
        self._owner_task: asyncio.Task[None] | None = None
        self._started: asyncio.Future[list[Any]] | None = None
        self._closed = False

    @property
    # 方法作用：暴露 owner task 供关闭验收与诊断使用。
    # Args: self - 当前连接。
    # Returns: owner task；尚未启动时为 None。
    def owner_task(self) -> asyncio.Task[None] | None:
        """返回持有本连接的 owner task。"""
        return self._owner_task

    @property
    # 方法作用：报告连接是否已关闭或正在关闭。
    # Args: self - 当前连接。
    # Returns: 已关闭返回 True。
    def is_closed(self) -> bool:
        """返回连接关闭状态。"""
        return self._closed

    # 方法作用：创建 owner task 并等待其完成连接建立。
    # Args: self - 当前连接；timeout - 建立连接的最长等待秒数。
    # Returns: MCP 工具描述列表；失败或超时抛出对应异常。
    async def start(self, timeout: float | None = None) -> list[Any]:  # noqa: ASYNC109  兼容 spec/56 §4 连接建立契约参数名，等待在 owner task 内有界
        """在 owner task 内建立连接并返回工具描述。"""
        if self._owner_task is not None:
            raise RuntimeError(f"MCP 连接 {self.name} 已启动")
        logger.debug("启动 MCP owner 连接入口", server=self.name, timeout=timeout)
        loop = asyncio.get_running_loop()
        self._started = loop.create_future()
        self._owner_task = asyncio.create_task(
            self._run(), name=f"mcp-owner:{self.name}",
        )
        try:
            tools = await asyncio.wait_for(asyncio.shield(self._started), timeout)
        except BaseException:
            # 超时、取消或建立失败：放弃该连接并确保栈退出发生在 owner task 内。
            await self.close(timeout=0.0)
            raise
        logger.info(
            "启动 MCP owner 连接完成", server=self.name, tools=len(tools),
        )
        return tools

    # 方法作用：通过 owner task 转发一次工具调用。
    # Args: self - 当前连接；tool_name - 工具名；arguments - 结构化参数；
    #       timeout - 本次调用等待上限秒数。
    # Returns: MCP 调用结果；连接已关闭时抛 MCPConnectionClosedError。
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  兼容 spec/56 §4 调用契约参数名，等待在 owner task 内有界
    ) -> Any:
        """在 owner task 内执行 MCP 工具调用。"""
        return await self._request(_OP_CALL, (tool_name, dict(arguments or {})), timeout)

    # 方法作用：通过 owner task 转发一次健康探测。
    # Args: self - 当前连接；timeout - 等待上限秒数。
    # Returns: 无返回值；连接已关闭时抛 MCPConnectionClosedError。
    async def send_ping(self, *, timeout: float | None = None) -> None:  # noqa: ASYNC109  兼容 spec/56 §4 ping 契约参数名，等待在 owner task 内有界
        """在 owner task 内执行 MCP ping。"""
        await self._request(_OP_PING, None, timeout)

    # 方法作用：投递请求并等待 owner task 完成，保证取消不悬挂 future。
    # Args: self - 当前连接；op - 操作类型；payload - 操作参数；timeout - 等待上限。
    # Returns: 操作结果；已关闭时抛 MCPConnectionClosedError。
    async def _request(
        self,
        op: str,
        payload: Any,
        timeout: float | None,  # noqa: ASYNC109  兼容 spec/56 §4 请求转发契约参数名，future 等待有界
    ) -> Any:
        if self._closed or self._owner_task is None:
            raise MCPConnectionClosedError(f"MCP 连接 {self.name} 已关闭")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put((op, payload, future))
        return await asyncio.wait_for(future, timeout)

    # 方法作用：协作式关闭 owner task，超时后强制取消并等待有界时长。
    # Args: self - 当前连接；timeout - 协作关闭等待上限秒数。
    # Returns: 在预算内干净关闭返回 True；强制取消返回 False；关闭幂等。
    async def close(self, timeout: float | None = _DEFAULT_CLOSE_TIMEOUT) -> bool:  # noqa: ASYNC109  兼容 spec/56 §4 关闭契约参数名，协作关闭与取消收尾均有界
        """关闭连接并保证栈退出发生在 owner task 内。"""
        task = self._owner_task
        if task is None or task.done():
            self._closed = True
            return True
        logger.debug("关闭 MCP owner 连接入口", server=self.name, timeout=timeout)
        await self._queue.put((_OP_SHUTDOWN, None, None))
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            # 调用方被取消：owner task 不能因无人等待而悬挂，须在有界宽限内
            # 等它完成栈退出与未完成请求收尾，再向上传播取消。
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=_CANCEL_GRACE_SECONDS)
            except asyncio.CancelledError:
                # 二次取消：放弃继续等待，但取消仍向上传播。
                logger.warning("关闭 MCP owner 连接等待被二次取消", server=self.name)
            self._closed = True
            logger.info("MCP owner 连接在取消中完成收尾", server=self.name)
            raise
        if task in done:
            self._closed = True
            logger.info("关闭 MCP owner 连接完成", server=self.name)
            return True
        task.cancel()
        await asyncio.wait({task}, timeout=_CANCEL_GRACE_SECONDS)
        self._closed = True
        logger.info("MCP owner 连接已强制关闭", server=self.name)
        return False

    # 方法作用：owner task 主循环：建立连接、服务请求、在自身任务内退出栈。
    # Args: self - 当前连接。
    # Returns: 无返回值；异常与取消在 owner task 内收尾后再结束。
    async def _run(self) -> None:
        active: asyncio.Future[Any] | None = None
        try:
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(self._transport)
                session = await stack.enter_async_context(
                    self._session_factory(self.name, streams)
                )
                await session.initialize()
                tool_list = (await session.list_tools()).tools
                if self._started is not None and not self._started.done():
                    self._started.set_result(list(tool_list))
                while True:
                    op, payload, future = await self._queue.get()
                    if op == _OP_SHUTDOWN:
                        break
                    if future is None or future.cancelled():
                        continue
                    active = future
                    try:
                        if op == _OP_CALL:
                            tool_name, arguments = payload
                            result = await session.call_tool(
                                tool_name, arguments=arguments,
                            )
                        else:
                            await session.send_ping()
                            result = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
                    else:
                        if not future.done():
                            future.set_result(result)
                    finally:
                        # 只有结果已回填才清除 active；取消路径必须保留该引用，
                        # 交给外层 finally 的 _fail_pending 显式失败，避免调用方永久悬挂。
                        if future.done():
                            active = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 只在实际处理边界记录一次：固定文案 + error_type + 原堆栈，
            # 异常原文（可能含连接凭证）不落盘；原始异常仍原样交给 start 等待者。
            logger.error(
                "MCP owner 连接异常结束",
                server=self.name,
                error_type=type(exc).__name__,
                exc_info=_safe_exc_info(exc, "MCP owner 连接异常结束，原始异常正文不记录"),
            )
            # 跨层打标：该异常已在本边界记录，manager/路由不得重复写同一堆栈。
            mark_error_logged(exc)
            if self._started is not None and not self._started.done():
                self._started.set_exception(exc)
        finally:
            self._closed = True
            self._fail_started()
            self._fail_pending(active)

    # 方法作用：显式失败仍未完成的 start 等待者，避免其永久悬挂。
    # Args: self - 当前连接。
    # Returns: 无返回值；_started 已完成或从未创建时不做任何事。
    def _fail_started(self) -> None:
        future = self._started
        if future is None or future.done():
            return
        future.set_exception(MCPConnectionClosedError(f"MCP 连接 {self.name} 已关闭"))
        # 上游可能已放弃等待（超时/取消）：标记异常已取回，避免 never-retrieved 噪音。
        future.exception()

    # 方法作用：把队列中与执行中的未完成请求显式失败，避免悬挂 future。
    # Args: self - 当前连接；active - 正在执行但未回填结果的请求 future。
    # Returns: 无返回值。
    def _fail_pending(self, active: asyncio.Future[Any] | None) -> None:
        futures: list[asyncio.Future[Any]] = []
        while True:
            try:
                _op, _payload, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if future is not None:
                futures.append(future)
        if active is not None:
            futures.append(active)
        for future in futures:
            if not future.done():
                future.set_exception(
                    MCPConnectionClosedError(f"MCP 连接 {self.name} 已关闭"),
                )
