"""MCP 重置生命周期的确定性测试替身：假 transport、假会话与轮询工具。

这些替身只服务 owner task 关闭契约与 reset 生命周期测试，不替代真实
stdio 链路的验收（真实进程用例见 test_reset_stdio_real.py）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：在限时内轮询等待条件成立，用于并发时序断言。
# Args: predicate - 条件函数；timeout - 最长等待秒数。
# Returns: 无返回值，超时时抛 AssertionError。
async def wait_until(predicate: Any, timeout: float = 5.0) -> None:  # noqa: ASYNC109  外部验收按 timeout= 关键字调用，参数名不可改；轮询自带截止检查
    logger.debug("等待条件入口", timeout=timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)
    logger.debug("等待条件完成", timeout=timeout)


class FakeMCPTool:
    """MCP 工具描述替身：携带 inputSchema 协议字段。"""

    # 方法作用：构造固定名称与描述的 MCP 工具描述。
    # Args: name - 工具名；description - 工具说明。
    # Returns: 无返回值。
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self.inputSchema: dict[str, Any] = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": [],
        }


class FakeClientSession:
    """MCP ClientSession 替身：记录进入/退出任务并支持门闩与故障注入。"""

    # 方法作用：初始化会话替身并配置工具清单与阻塞点。
    # Args: streams - transport 流对；tools - 暴露的工具名；call_gate - 调用前等待事件；
    #       init_gate - initialize 前等待事件；init_error - initialize 注入异常。
    # Returns: 无返回值。
    def __init__(
        self,
        *streams: Any,
        tools: list[str] | None = None,
        call_gate: asyncio.Event | None = None,
        init_gate: asyncio.Event | None = None,
        init_error: BaseException | None = None,
    ) -> None:
        self.streams = streams
        self.tool_names = list(tools or ["echo"])
        self.call_gate = call_gate
        self.init_gate = init_gate
        self.init_error = init_error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.call_task: asyncio.Task[Any] | None = None
        self.closed = False
        self.enter_task: asyncio.Task[Any] | None = None
        self.exit_task: asyncio.Task[Any] | None = None
        self.enter_count = 0

    # 方法作用：记录进入会话的任务与次数。
    # Args: self - 当前会话。
    # Returns: 当前会话自身。
    async def __aenter__(self) -> "FakeClientSession":
        self.enter_task = asyncio.current_task()
        self.enter_count += 1
        return self

    # 方法作用：记录退出会话的任务并标记关闭。
    # Args: self - 当前会话；exc_info - 上下文异常三元组。
    # Returns: False，表示不吞异常。
    async def __aexit__(self, *exc_info: Any) -> bool:
        self.exit_task = asyncio.current_task()
        self.closed = True
        return False

    # 方法作用：模拟 MCP 会话初始化，可经门闩阻塞或注入失败。
    # Args: self - 当前会话。
    # Returns: 无返回值。
    async def initialize(self) -> None:
        if self.init_gate is not None:
            await self.init_gate.wait()
        if self.init_error is not None:
            raise self.init_error
        return None

    # 方法作用：返回当前轮次的工具描述清单。
    # Args: self - 当前会话。
    # Returns: 带 tools 属性的协议对象。
    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[FakeMCPTool(name) for name in self.tool_names])

    # 方法作用：记录并回显一次工具调用，可经门闩阻塞。
    # Args: self - 当前会话；name - 工具名；arguments - 调用参数。
    # Returns: MCP 内容块协议对象。
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.call_task = asyncio.current_task()
        if self.call_gate is not None:
            await self.call_gate.wait()
        self.calls.append((name, dict(arguments or {})))
        text = str((arguments or {}).get("text", ""))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            isError=False,
        )

    # 方法作用：模拟 MCP 健康探测。
    # Args: self - 当前会话。
    # Returns: 无返回值。
    async def send_ping(self) -> None:
        return None


class FakeTransport:
    """transport 替身：记录进入/退出任务，支持门闩、失败与真实 anyio scope。"""

    # 方法作用：配置 transport 的阻塞、失败与作用域行为。
    # Args: enter_gate - 进入前等待的事件；exit_delay - 退出耗时；fail_on_enter - 进入即失败；
    #       enter_error - 进入时抛出的自定义异常（优先于 fail_on_enter，供敏感正文回归）；
    #       anyio_scope - 进入真实 anyio CancelScope（跨任务退出会抛 RuntimeError）。
    # Returns: 无返回值。
    def __init__(
        self,
        *,
        enter_gate: asyncio.Event | None = None,
        exit_delay: float = 0.0,
        fail_on_enter: bool = False,
        enter_error: BaseException | None = None,
        anyio_scope: bool = False,
    ) -> None:
        self.enter_gate = enter_gate
        self.exit_delay = exit_delay
        self.fail_on_enter = fail_on_enter
        self.enter_error = enter_error
        self.anyio_scope = anyio_scope
        self.enter_task: asyncio.Task[Any] | None = None
        self.exit_task: asyncio.Task[Any] | None = None
        self.enter_count = 0
        self.exit_count = 0
        self._scope: Any = None

    # 方法作用：记录进入任务并可选进入 anyio cancel scope。
    # Args: self - 当前 transport。
    # Returns: 两条流替身；配置了 enter_error 或 fail_on_enter 时抛出对应异常。
    async def __aenter__(self) -> tuple[Any, Any]:
        self.enter_task = asyncio.current_task()
        self.enter_count += 1
        if self.enter_gate is not None:
            await self.enter_gate.wait()
        if self.enter_error is not None:
            raise self.enter_error
        if self.fail_on_enter:
            raise RuntimeError("transport 建立失败")
        if self.anyio_scope:
            import anyio

            self._scope = anyio.CancelScope()
            self._scope.__enter__()
        return (object(), object())

    # 方法作用：记录退出任务并退出任何作用域，跨任务退出会抛错。
    # Args: self - 当前 transport；exc_info - 上下文异常三元组。
    # Returns: False，表示不吞异常。
    async def __aexit__(self, *exc_info: Any) -> bool:
        self.exit_task = asyncio.current_task()
        self.exit_count += 1
        if self.exit_delay:
            await asyncio.sleep(self.exit_delay)
        if self._scope is not None:
            self._scope.__exit__(*exc_info)
            self._scope = None
        return False
