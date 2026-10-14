"""真实本地 stdio MCP 链路：跨任务 reset、竞态与关闭。

覆盖 spec/56 §8：真实子进程链路不得靠 Mock 掩盖跨任务关闭错误；
每个用例用 `sys.executable` 启动本地 fixture Server，不访问任何远程服务。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_mcp.lifecycle_fakes import wait_until

logger = logging.getLogger(__name__)

FIXTURE_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "stdio_echo_server.py"


# 方法作用：构造指向本地 stdio fixture 进程的 manager 与配置。
# Args: 无。
# Returns: (manager, 内部服务名)。
def make_stdio_manager() -> tuple[Any, str]:
    from src.mcp_client.client_manager import MCPClientManager

    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE_SERVER)],
        "enabled": True,
    }
    manager = MCPClientManager()
    manager._configured_system_servers = {"stdio_echo": config}
    manager._server_configs = {"stdio_echo": dict(config)}
    return manager, "stdio_echo"


# 方法作用：检查指定进程是否仍存活（Windows tasklist，不引入新依赖）。
# Args: pid - 进程号。
# Returns: 进程存在返回 True。
def process_alive(pid: int) -> bool:
    completed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
        capture_output=True, text=True, errors="replace", timeout=20, check=False,
    )
    return completed.stdout.lstrip().startswith('"')


# 方法作用：通过已连接的 get_pid 工具读取 fixture 进程号。
# Args: manager - MCPClientManager；name - 内部服务名。
# Returns: fixture 子进程 pid。
async def read_server_pid(manager: Any, name: str) -> int:
    text = await manager.langchain_tools[f"{name}__get_pid"].ainvoke({})
    return int(str(text).strip())


class TestRealStdioReset:
    """覆盖 spec/56 §8：真实 stdio 进程跨任务 reset/health/竞态/关闭。"""

    # 方法作用：验证真实 stdio 连接可跨任务重置、替换子进程并在关闭后退出。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.integration
    async def test_cross_task_reset_and_close(self) -> None:
        """连接、健康探测、重置与关闭分别发生在不同任务。"""
        logger.debug("test_cross_task_reset_and_close 入口")
        manager, name = make_stdio_manager()
        old_pid = -1
        new_pid = -1
        try:
            await manager._connect_single(
                name,
                manager._configured_system_servers[name],
                scope="system",
                tenant_id=0,
                owner_user_id=0,
                timeout=30,
            )
            old_pid = await read_server_pid(manager, name)
            assert process_alive(old_pid) is True
            tool_key = f"{name}__echo"
            assert await manager.langchain_tools[tool_key].ainvoke({"text": "before"}) == "before"

            await asyncio.create_task(manager.health_check())
            result = await asyncio.create_task(manager.reset_server(name, timeout=30))

            assert result == {"scope": "system", "tools": 2}
            new_pid = await read_server_pid(manager, name)
            assert new_pid != old_pid
            await wait_until(lambda: not process_alive(old_pid), timeout=10)
            assert await manager.langchain_tools[tool_key].ainvoke({"text": "after"}) == "after"
        finally:
            await asyncio.create_task(manager.close_all())

        assert manager._connections == {}
        assert manager.sessions == {}
        if new_pid > 0:
            await wait_until(lambda: not process_alive(new_pid), timeout=10)
        logger.info("test_cross_task_reset_and_close 完成")

    # 方法作用：验证真实 stdio 进程在 reset 与 remove 竞态后不残留连接。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.integration
    async def test_reset_remove_race_cleans_real_process(self) -> None:
        """竞态结束后不得留下会话、工具、owner task 或子进程。"""
        logger.debug("test_reset_remove_race_cleans_real_process 入口")
        manager, name = make_stdio_manager()
        try:
            await manager._connect_single(
                name,
                manager._configured_system_servers[name],
                scope="system",
                tenant_id=0,
                owner_user_id=0,
                timeout=30,
            )
            first_pid = await read_server_pid(manager, name)
            owner_task = manager._connections[name].owner_task

            reset_task = asyncio.create_task(manager.reset_server(name, timeout=30))
            await asyncio.sleep(0.05)
            remove_task = asyncio.create_task(manager.remove_server(name))
            await asyncio.wait_for(
                asyncio.gather(reset_task, remove_task, return_exceptions=True),
                60,
            )

            assert manager._connections == {}
            assert manager.sessions == {}
            assert manager.langchain_tools == {}
            assert owner_task is not None and owner_task.done() is True
            await wait_until(lambda: not process_alive(first_pid), timeout=10)
        finally:
            await asyncio.create_task(manager.close_all())
        logger.info("test_reset_remove_race_cleans_real_process 完成")

    # 方法作用：验证重置超时后真实进程可被清理并允许再次重置成功。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.integration
    async def test_failed_reset_leaves_retryable_config(self) -> None:
        """超时预算过小的失败必须清理旧进程、保留配置且可重试。"""
        logger.debug("test_failed_reset_leaves_retryable_config 入口")
        from src.mcp_client.client_manager import MCPResetTimeoutError

        manager, name = make_stdio_manager()
        try:
            await manager._connect_single(
                name,
                manager._configured_system_servers[name],
                scope="system",
                tenant_id=0,
                owner_user_id=0,
                timeout=30,
            )
            first_pid = await read_server_pid(manager, name)

            with pytest.raises(MCPResetTimeoutError):
                await manager.reset_server(name, timeout=0.001)

            assert manager.sessions == {}
            assert manager.langchain_tools == {}
            assert manager._server_configs[name] == manager._configured_system_servers[name]
            await wait_until(lambda: not process_alive(first_pid), timeout=10)

            result = await manager.reset_server(name, timeout=30)
            assert result["tools"] == 2
            assert await manager.langchain_tools[f"{name}__echo"].ainvoke(
                {"text": "retry"},
            ) == "retry"
        finally:
            await asyncio.create_task(manager.close_all())
        logger.info("test_failed_reset_leaves_retryable_config 完成")

    # 方法作用：验证建立连接期间取消 reset 后真实进程被清理且可再次恢复。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.integration
    async def test_cancelled_reset_recovers_with_real_process(self) -> None:
        """取消不得留下半初始化会话、工具或旧子进程，随后必须可恢复。"""
        logger.debug("test_cancelled_reset_recovers_with_real_process 入口")
        manager, name = make_stdio_manager()
        try:
            await manager._connect_single(
                name,
                manager._configured_system_servers[name],
                scope="system",
                tenant_id=0,
                owner_user_id=0,
                timeout=30,
            )
            first_pid = await read_server_pid(manager, name)
            slow_config = dict(manager._configured_system_servers[name])
            slow_config["env"] = {"MCP_FIXTURE_STARTUP_DELAY": "30"}
            manager._configured_system_servers[name] = slow_config
            manager._server_configs[name] = dict(slow_config)

            task = asyncio.create_task(manager.reset_server(name, timeout=30))
            await asyncio.sleep(0.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)

            assert manager.sessions == {}
            assert manager.langchain_tools == {}
            assert manager._server_configs[name] == slow_config
            await wait_until(lambda: not process_alive(first_pid), timeout=10)

            recovered = {
                key: value for key, value in slow_config.items() if key != "env"
            }
            manager._configured_system_servers[name] = recovered
            manager._server_configs[name] = dict(recovered)
            result = await manager.reset_server(name, timeout=30)

            assert result["tools"] == 2
            assert await manager.langchain_tools[f"{name}__echo"].ainvoke(
                {"text": "recovered"},
            ) == "recovered"
        finally:
            await asyncio.create_task(manager.close_all())
        logger.info("test_cancelled_reset_recovers_with_real_process 完成")
