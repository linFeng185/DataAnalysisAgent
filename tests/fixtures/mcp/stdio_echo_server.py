"""真实本地 stdio MCP 进程：提供 echo 工具，供跨任务 reset/关闭验收使用。

由测试以 `sys.executable` 启动，只在本机以 stdio 通信，不依赖任何远程服务。
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("reset-fixture")


# 方法作用：原样返回输入文本，作为可断言的确定性工具。
# Args: text - 任意文本。
# Returns: 与输入相同的文本。
@mcp.tool()
def echo(text: str) -> str:
    """原样返回输入文本。"""
    return text


# 方法作用：返回本进程号，供测试验证旧子进程已被清理。
# Args: 无。
# Returns: 当前进程号字符串。
@mcp.tool()
def get_pid() -> str:
    """返回本进程号的字符串形式。"""
    return str(os.getpid())


if __name__ == "__main__":
    # 可选启动延迟（秒）：让取消类用例确定性地在建立连接阶段中断。
    _delay = float(os.environ.get("MCP_FIXTURE_STARTUP_DELAY", "0") or 0)
    if _delay > 0:
        import time

        time.sleep(_delay)
    mcp.run()
