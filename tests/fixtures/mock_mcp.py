"""无需外部进程的静态 MCP 测试 Server。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any


class StaticMCPTestServer:
    """实现 MCP ClientSession 测试所需的 list/call/ping 最小协议。"""

    # 方法作用：创建带 echo 工具的内存 MCP Server。
    # Args: self - 当前 Server。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self._tools: dict[str, tuple[str, dict[str, Any], Callable[..., Any]]] = {}
        self.register_tool(
            "echo",
            "原样返回输入文本",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            lambda text: text,
        )

    # 方法作用：向内存 Server 注册固定工具。
    # Args: self - 当前 Server；name/description/input_schema - 工具协议；handler - 调用函数。
    # Returns: 无返回值。
    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        self._tools[name] = (description, dict(input_schema), handler)

    # 方法作用：返回所有已注册工具的 MCP 风格描述。
    # Args: self - 当前 Server。
    # Returns: 带 tools 属性的协议对象。
    async def list_tools(self) -> SimpleNamespace:
        tools = [
            SimpleNamespace(name=name, description=description, inputSchema=schema)
            for name, (description, schema, _) in self._tools.items()
        ]
        return SimpleNamespace(tools=tools)

    # 方法作用：调用指定工具并返回 MCP 文本内容块。
    # Args: self - 当前 Server；name - 工具名；arguments - 结构化参数。
    # Returns: 带 content/isError 的协议对象。
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        if name not in self._tools:
            raise KeyError(f"未知 MCP 工具: {name}")
        handler = self._tools[name][2]
        value = handler(**dict(arguments or {}))
        if inspect.isawaitable(value):
            value = await value
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=str(value))],
            isError=False,
        )

    # 方法作用：模拟 MCP Server 健康探测。
    # Args: self - 当前 Server。
    # Returns: 无返回值。
    async def send_ping(self) -> None:
        return None
