"""MCP reset 生命周期：并发、竞态、超时、取消、失败恢复与工具清理。

覆盖 spec/56 §3/§5：按服务串行、跨服务并行、epoch 防复活、失败可重试、
超时与取消不留半初始化状态，以及重置后旧工具彻底失效。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.test_mcp.lifecycle_fakes import FakeClientSession, FakeTransport, wait_until

logger = logging.getLogger(__name__)


class SignalingTransport(FakeTransport):
    """进入时互相通知的 transport：两个 reset 串行执行会超时失败。"""

    # 方法作用：配置自身与他人进入事件。
    # Args: signal - 本 transport 进入时置位；wait_for - 等待另一个 transport 进入。
    # Returns: 无返回值。
    def __init__(self, signal: asyncio.Event, wait_for: asyncio.Event) -> None:
        super().__init__()
        self._signal = signal
        self._wait_for = wait_for

    # 方法作用：通知对端并等待对端也已进入。
    # Args: self - 当前 transport。
    # Returns: 两条流替身。
    async def __aenter__(self) -> tuple[Any, Any]:
        self._signal.set()
        await asyncio.wait_for(self._wait_for.wait(), 2)
        return await super().__aenter__()


class ResetEnv:
    """按服务名排队提供 transport 与会话的确定性重置测试环境。"""

    # 方法作用：装配 manager 替身、认证身份与配置工厂。
    # Args: monkeypatch - pytest 替身工具。
    # Returns: 无返回值。
    def __init__(self, monkeypatch: Any) -> None:
        from src.mcp_client.client_manager import MCPClientManager

        self.manager = MCPClientManager()
        self.built: list[str] = []
        self.sessions: dict[str, list[FakeClientSession]] = {}
        self._transports: dict[str, list[FakeTransport]] = {}
        self._tool_sets: dict[str, list[list[str]]] = {}
        self._name_by_url: dict[str, str] = {}
        monkeypatch.setattr(
            self.manager, "_build_transport_context", self._build_transport,
        )
        monkeypatch.setattr(self.manager, "_session_factory", self._build_session)
        monkeypatch.setattr(
            "src.mcp_client.client_manager._is_managed_remote_config_allowed",
            lambda config: True,
        )
        monkeypatch.setattr(self.manager, "_reconnect_backoff_wait", AsyncMock())
        monkeypatch.setattr(self.manager, "authorize_runtime_server", AsyncMock())
        monkeypatch.setattr("src.api.auth.get_current_role", lambda: "tenant_admin")
        monkeypatch.setattr("src.api.auth.get_current_tenant_id", lambda: 2)
        monkeypatch.setattr("src.api.auth.get_current_user_id", lambda: 7)

    # 方法作用：为下一个连接轮次排队 transport 与工具清单。
    # Args: name - 内部 Server 名；transport - transport 替身；tools - 该轮工具名。
    # Returns: 无返回值。
    def queue(
        self,
        name: str,
        transport: FakeTransport,
        tools: list[str] | None = None,
    ) -> None:
        self._transports.setdefault(name, []).append(transport)
        self._tool_sets.setdefault(name, []).append(list(tools or ["echo"]))
        self._name_by_url[f"https://{name}.example.com/sse"] = name

    # 方法作用：返回受管 SSE 服务的连接配置。
    # Args: name - 内部 Server 名。
    # Returns: 连接配置字典。
    def config(self, name: str) -> dict:
        return {"transport": "sse", "url": f"https://{name}.example.com/sse"}

    # 方法作用：通过真实 _connect_single 建立初始连接。
    # Args: name - 内部 Server 名；scope - 作用域；tenant_id - 租户；owner_user_id - 所有者。
    # Returns: 无返回值。
    async def connect(
        self,
        name: str,
        *,
        scope: str = "private",
        tenant_id: int = 2,
        owner_user_id: int = 7,
    ) -> None:
        config = self.config(name)
        # 与 connect_all/reload_from_db 相同：先注册最近合法配置，再建立连接。
        self.manager._server_configs[name] = config
        await self.manager._connect_single(
            name,
            config,
            scope=scope,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )

    # 方法作用：按配置对应的服务名返回下一个 transport 替身。
    # Args: config - 连接配置。
    # Returns: transport 替身。
    def _build_transport(self, config: dict) -> FakeTransport:
        name = self._name_by_url[config["url"]]
        self.built.append(name)
        return self._transports[name].pop(0)

    # 方法作用：按轮次创建并记录假会话。
    # Args: name - 内部 Server 名；streams - transport 流对。
    # Returns: 该轮次的假会话。
    def _build_session(self, name: str, streams: Any) -> FakeClientSession:
        session = FakeClientSession(*streams, tools=self._tool_sets[name].pop(0))
        self.sessions.setdefault(name, []).append(session)
        return session

    # 方法作用：读取某服务当前的工具键集合。
    # Args: name - 内部 Server 名。
    # Returns: 该服务的工具键列表。
    def tool_keys(self, name: str) -> list[str]:
        return [key for key in self.manager.langchain_tools if key.startswith(f"{name}__")]


class TestMCPResetSuccess:
    """覆盖 spec/56 §5：成功原子替换、工具清理与降级恢复。"""

    # 方法作用：验证 reset 原子替换会话与工具并清理旧工具。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_replaces_session_and_tools(self, monkeypatch) -> None:
        """新工具必须绑定新会话，旧会话与旧工具必须失效。"""
        logger.debug("test_reset_replaces_session_and_tools 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo", "search"])
        await env.connect(name)
        old_connection = env.manager.sessions[name]
        old_tool = env.manager.langchain_tools[f"{name}__echo"]

        env.queue(name, FakeTransport(), ["echo"])
        result = await env.manager.reset_server(name, timeout=5)

        assert result == {"scope": "private", "tools": 1}
        assert env.manager.sessions[name] is not old_connection
        assert env.sessions[name][0].closed is True
        assert env.tool_keys(name) == [f"{name}__echo"]
        new_tool = env.manager.langchain_tools[f"{name}__echo"]
        assert new_tool is not old_tool
        assert await new_tool.ainvoke({"text": "hi"}) == "hi"
        assert env.sessions[name][-1].calls == [("echo", {"text": "hi"})]
        logger.info("test_reset_replaces_session_and_tools 完成")

    # 方法作用：验证重置过程中旧工具先被摘除，不存在新旧混合状态。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_removes_old_tools_before_new_connect(self, monkeypatch) -> None:
        """拆除阶段与上线阶段之间必须没有该服务的工具或会话。"""
        logger.debug("test_reset_removes_old_tools_before_new_connect 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        gate = asyncio.Event()
        env.queue(name, FakeTransport(enter_gate=gate), ["echo", "search"])

        task = asyncio.create_task(env.manager.reset_server(name, timeout=10))
        await wait_until(lambda: env.built.count(name) == 2)

        assert name not in env.manager.sessions
        assert env.tool_keys(name) == []

        gate.set()
        await asyncio.wait_for(task, 5)
        assert env.tool_keys(name) == [f"{name}__echo", f"{name}__search"]
        logger.info("test_reset_removes_old_tools_before_new_connect 完成")

    # 方法作用：验证路由传入的已授权配置会刷新最近合法配置并被使用。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_adopts_authorized_config(self, monkeypatch) -> None:
        """数据库 URL 更新后，重置必须使用路由校验过的新配置。"""
        logger.debug("test_reset_adopts_authorized_config 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        updated = env.config(name)
        env.queue(name, FakeTransport(), ["echo", "search"])

        result = await env.manager.reset_server(
            name, timeout=5, authorized_config=updated,
        )

        assert result["tools"] == 2
        assert env.manager._server_configs[name] == updated
        logger.info("test_reset_adopts_authorized_config 完成")

    # 方法作用：验证降级（无会话）的服务仅对 reset 可见并可恢复。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_degraded_server_resolves_only_for_reset(self, monkeypatch) -> None:
        """重连耗尽后仍保留合法配置，reset 能解析并恢复连接。"""
        logger.debug("test_degraded_server_resolves_only_for_reset 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        await env.manager._degrade_locked(name)

        assert env.manager.resolve_loaded_server(name) is None
        assert env.manager.resolve_resettable_server(name) == (name, "private")

        env.queue(name, FakeTransport(), ["echo"])
        result = await env.manager.reset_server(name, timeout=5)

        assert result["tools"] == 1
        assert env.manager.sessions[name] is env.manager._connections[name]
        assert env.sessions[name][-1].closed is False
        logger.info("test_degraded_server_resolves_only_for_reset 完成")

    # 方法作用：验证健康检查会尝试恢复降级服务的连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_health_check_restores_degraded_server(self, monkeypatch) -> None:
        """degraded 服务在下一轮健康检查自动回填工具与连接。"""
        logger.debug("test_health_check_restores_degraded_server 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        await env.manager._degrade_locked(name)
        env.queue(name, FakeTransport(), ["echo", "search"])

        await env.manager.health_check()

        assert env.manager.sessions[name] is env.manager._connections[name]
        assert env.sessions[name][-1].closed is False
        assert env.tool_keys(name) == [f"{name}__echo", f"{name}__search"]
        logger.info("test_health_check_restores_degraded_server 完成")

    # 方法作用：验证内置 YAML 服务使用配置文件重置连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_builtin_system_reset_uses_yaml_config(self, monkeypatch) -> None:
        """内置 system 服务不在数据库，使用受信任 YAML 配置重建。"""
        logger.debug("test_builtin_system_reset_uses_yaml_config 入口")
        env = ResetEnv(monkeypatch)
        name = "builtin_docs"
        config = {"transport": "sse", "url": f"https://{name}.example.com/sse",
                  "enabled": True}
        env.manager._configured_system_servers = {name: config}
        env.queue(name, FakeTransport(), ["echo", "search"])

        result = await env.manager.reset_server(name, timeout=5)

        assert result == {"scope": "system", "tools": 2}
        assert env.manager.sessions[name] is env.manager._connections[name]
        assert env.sessions[name][-1].closed is False
        logger.info("test_builtin_system_reset_uses_yaml_config 完成")


class TestMCPResetConcurrency:
    """覆盖 spec/56 §3：按服务串行、跨服务并行、竞态不复活。"""

    # 方法作用：验证同一服务的并发 reset 严格串行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_same_server_resets_are_serialized(self, monkeypatch) -> None:
        """第二个 reset 必须在第一个提交后才开始建立连接。"""
        logger.debug("test_same_server_resets_are_serialized 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        gate = asyncio.Event()
        env.queue(name, FakeTransport(enter_gate=gate), ["echo"])
        env.queue(name, FakeTransport(), ["echo", "search"])

        first = asyncio.create_task(env.manager.reset_server(name, timeout=10))
        await wait_until(lambda: env.built.count(name) == 2)
        second = asyncio.create_task(env.manager.reset_server(name, timeout=10))
        await asyncio.sleep(0.05)

        assert env.built.count(name) == 2
        gate.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 5)

        assert env.built.count(name) == 3
        assert results[0]["tools"] == 1
        assert results[1]["tools"] == 2
        assert env.manager.sessions[name] is env.manager._connections[name]
        assert env.sessions[name][-1].closed is False
        logger.info("test_same_server_resets_are_serialized 完成")

    # 方法作用：验证不同服务的 reset 真正并行执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_different_servers_reset_in_parallel(self, monkeypatch) -> None:
        """两个服务必须能同时处于建立连接阶段。"""
        logger.debug("test_different_servers_reset_in_parallel 入口")
        env = ResetEnv(monkeypatch)
        first_name, second_name = "private_2_7_alpha", "private_2_7_beta"
        for server_name in (first_name, second_name):
            env.queue(server_name, FakeTransport(), ["echo"])
            await env.connect(server_name)
        entered = {first_name: asyncio.Event(), second_name: asyncio.Event()}
        env.queue(
            first_name,
            SignalingTransport(entered[first_name], entered[second_name]),
            ["echo"],
        )
        env.queue(
            second_name,
            SignalingTransport(entered[second_name], entered[first_name]),
            ["echo"],
        )

        results = await asyncio.wait_for(
            asyncio.gather(
                env.manager.reset_server(first_name, timeout=10),
                env.manager.reset_server(second_name, timeout=10),
            ),
            5,
        )

        assert [result["tools"] for result in results] == [1, 1]
        logger.info("test_different_servers_reset_in_parallel 完成")

    # 方法作用：验证 reset 与 remove 并发时终态为已删除，不复活服务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_race_with_remove_keeps_server_removed(self, monkeypatch) -> None:
        """删除请求与重置并发时，最终必须没有任何运行时残留。"""
        logger.debug("test_reset_race_with_remove_keeps_server_removed 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        gate = asyncio.Event()
        env.queue(name, FakeTransport(enter_gate=gate), ["echo"])

        reset_task = asyncio.create_task(env.manager.reset_server(name, timeout=10))
        await wait_until(lambda: env.built.count(name) == 2)
        remove_task = asyncio.create_task(env.manager.remove_server(name))
        gate.set()
        await asyncio.wait_for(asyncio.gather(reset_task, remove_task), 5)

        assert name not in env.manager.sessions
        assert env.tool_keys(name) == []
        assert name not in env.manager._server_configs
        logger.info("test_reset_race_with_remove_keeps_server_removed 完成")

    # 方法作用：验证已删除的服务不会被 reset 重新创建。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_after_remove_does_not_resurrect(self, monkeypatch) -> None:
        """删除后到达的 reset 必须以不可用失败且不建连。"""
        logger.debug("test_reset_after_remove_does_not_resurrect 入口")
        from src.mcp_client.client_manager import MCPResetUnavailableError

        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        built_before = list(env.built)
        await env.manager.remove_server(name)

        with pytest.raises(MCPResetUnavailableError):
            await env.manager.reset_server(name, timeout=5)

        assert env.built == built_before
        assert name not in env.manager.sessions
        assert env.manager._server_configs.get(name) is None
        logger.info("test_reset_after_remove_does_not_resurrect 完成")

    # 方法作用：验证退避期间被删除的服务不会被重连复活。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reconnect_aborts_when_removed_during_backoff(self, monkeypatch) -> None:
        """重连退避窗口内的删除必须让重连放弃。"""
        logger.debug("test_reconnect_aborts_when_removed_during_backoff 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        built_before = list(env.built)

        # 方法作用：在重连退避期间模拟删除落地。
        # Args: retry - 重试轮次（忽略）。
        # Returns: 无返回值。
        async def remove_during_backoff(retry: int) -> None:
            del retry
            await env.manager.remove_server(name)

        monkeypatch.setattr(env.manager, "_reconnect_backoff_wait", remove_during_backoff)
        await env.manager._reconnect(name)

        assert env.built == built_before
        assert name not in env.manager.sessions
        assert name not in env.manager._server_configs
        logger.info("test_reconnect_aborts_when_removed_during_backoff 完成")

    # 方法作用：验证读库期间被删除的服务不会被 reload 复活。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reload_skips_server_removed_during_db_read(self, monkeypatch) -> None:
        """reload 先读库再连接时，删除必须优先于数据库快照。"""
        logger.debug("test_reload_skips_server_removed_during_db_read 入口")
        import src.memory.pg_pool as pool_module

        env = ResetEnv(monkeypatch)
        stale_name = "private_2_7_stale"
        fresh_name = "private_2_7_fresh"
        env.queue(stale_name, FakeTransport(), ["echo"])
        await env.connect(stale_name)
        env.queue(fresh_name, FakeTransport(), ["echo"])
        rows = [
            {"name": "stale", "scope": "private", "tenant_id": 2, "owner_user_id": 7,
             "transport": "sse", "command": "", "args": "",
             "url": f"https://{stale_name}.example.com/sse", "env_vars": {}},
            {"name": "fresh", "scope": "private", "tenant_id": 2, "owner_user_id": 7,
             "transport": "sse", "command": "", "args": "",
             "url": f"https://{fresh_name}.example.com/sse", "env_vars": {}},
        ]

        # 方法作用：在返回数据库快照前先模拟删除落地。
        # Args: args/kwargs - fetch 参数（忽略）。
        # Returns: 数据库行快照。
        async def fetch_with_concurrent_delete(*args: Any, **kwargs: Any) -> list[dict]:
            del args, kwargs
            await env.manager.remove_server(stale_name)
            return rows

        connection = SimpleNamespace(fetch=AsyncMock(side_effect=fetch_with_concurrent_delete))

        # 方法作用：提供带身份注入的数据库连接替身。
        # Args: kwargs - 连接身份参数（忽略）。
        # Returns: 测试连接。
        @asynccontextmanager
        async def scoped_connection(**kwargs: Any):
            del kwargs
            yield connection

        monkeypatch.setattr(pool_module, "pg_connection", scoped_connection)
        count = await env.manager.reload_from_db(tenant_id=2, user_id=7)

        assert count == 1
        assert stale_name not in env.manager.sessions
        assert env.manager.sessions[fresh_name] is env.manager._connections[fresh_name]
        assert env.sessions[fresh_name][-1].closed is False
        logger.info("test_reload_skips_server_removed_during_db_read 完成")

    # 方法作用：验证删除终态后路由的准备步骤不得重新注册该服务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prepare_rejected_after_remove(self, monkeypatch) -> None:
        """删除墓碑未被刷新前，prepare 必须拒绝复活且不建立连接。"""
        logger.debug("test_prepare_rejected_after_remove 入口")
        from src.mcp_client.client_manager import MCPResetUnavailableError

        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        await env.manager.remove_server(name)
        built_before = list(env.built)

        with pytest.raises(MCPResetUnavailableError):
            await env.manager.prepare_authorized_server(
                name,
                config=env.config(name),
                scope="private",
                tenant_id=2,
                owner_user_id=7,
            )

        assert env.built == built_before
        assert name not in env.manager._server_configs
        logger.info("test_prepare_rejected_after_remove 完成")

    # 方法作用：验证首次重置的未加载目标只登记元数据、不建立连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prepare_registers_fresh_target_without_connecting(self, monkeypatch) -> None:
        """合法新目标的准备不触发任何连接，随后由 reset 单独重建。"""
        logger.debug("test_prepare_registers_fresh_target_without_connecting 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_fresh"

        await env.manager.prepare_authorized_server(
            name,
            config=env.config(name),
            scope="private",
            tenant_id=2,
            owner_user_id=7,
        )

        assert env.built == []
        assert env.manager._server_configs[name] == env.config(name)
        assert env.manager._server_scopes[name] == "private"
        assert env.manager._server_tenants[name] == 2
        assert env.manager._server_owners[name] == 7
        logger.info("test_prepare_registers_fresh_target_without_connecting 完成")


class TestMCPResetFailure:
    """覆盖 spec/56 §5：失败、超时、取消与非法配置不得留下半初始化状态。"""

    # 方法作用：验证失败后保留合法配置且可以再次重置成功。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_failure_then_retry_recovers(self, monkeypatch) -> None:
        """失败不得保留半初始化或旧工具，但必须保留可重试配置。"""
        logger.debug("test_failure_then_retry_recovers 入口")
        from src.mcp_client.client_manager import MCPResetError

        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo", "search"])
        await env.connect(name)
        env.queue(name, FakeTransport(fail_on_enter=True), ["echo"])

        with pytest.raises(MCPResetError):
            await env.manager.reset_server(name, timeout=5)

        assert env.sessions[name][0].closed is True
        assert name not in env.manager.sessions
        assert env.tool_keys(name) == []
        assert env.manager._server_configs[name] == env.config(name)

        env.queue(name, FakeTransport(), ["echo"])
        result = await env.manager.reset_server(name, timeout=5)

        assert result["tools"] == 1
        assert env.manager.sessions[name] is env.manager._connections[name]
        assert env.sessions[name][-1].closed is False
        logger.info("test_failure_then_retry_recovers 完成")

    # 方法作用：验证重置超时受控且不留下半初始化连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_timeout_leaves_no_partial_state_and_recovers(self, monkeypatch) -> None:
        """超时必须清理半初始化连接、保留配置，并允许随后恢复。"""
        logger.debug("test_timeout_leaves_no_partial_state_and_recovers 入口")
        from src.mcp_client.client_manager import MCPResetTimeoutError

        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        gate = asyncio.Event()
        blocked = FakeTransport(enter_gate=gate)
        env.queue(name, blocked, ["echo"])

        with pytest.raises(MCPResetTimeoutError):
            await env.manager.reset_server(name, timeout=0.2)

        assert name not in env.manager.sessions
        assert env.tool_keys(name) == []
        assert env.manager._server_configs[name] == env.config(name)
        gate.set()
        await wait_until(lambda: blocked.enter_task is not None and blocked.enter_task.done())

        env.queue(name, FakeTransport(), ["echo"])
        result = await env.manager.reset_server(name, timeout=5)

        assert result["tools"] == 1
        logger.info("test_timeout_leaves_no_partial_state_and_recovers 完成")

    # 方法作用：验证取消重置后不残留会话、工具或 owner task。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_cancellation_cleans_partial_state(self, monkeypatch) -> None:
        """客户端断开取消请求时不得留下半初始化连接。"""
        logger.debug("test_cancellation_cleans_partial_state 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        gate = asyncio.Event()
        blocked = FakeTransport(enter_gate=gate)
        env.queue(name, blocked, ["echo"])

        task = asyncio.create_task(env.manager.reset_server(name, timeout=30))
        await wait_until(lambda: env.built.count(name) == 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert name not in env.manager.sessions
        assert env.tool_keys(name) == []
        assert env.manager._server_configs[name] == env.config(name)
        gate.set()
        await wait_until(lambda: blocked.enter_task is not None and blocked.enter_task.done())
        logger.info("test_cancellation_cleans_partial_state 完成")

    # 方法作用：验证非法受管配置被拒绝且不建立连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unsafe_managed_config_is_not_connected(self, monkeypatch) -> None:
        """受管服务退化为 stdio 时必须拒绝重置且不启动进程。"""
        logger.debug("test_unsafe_managed_config_is_not_connected 入口")
        from src.mcp_client.client_manager import MCPResetBlockedError

        env = ResetEnv(monkeypatch)
        monkeypatch.setattr(
            "src.mcp_client.client_manager._is_managed_remote_config_allowed",
            lambda config: False,
        )
        name = "private_2_7_docs"
        env.manager._server_configs[name] = {"transport": "stdio", "command": "python"}
        env.manager._server_scopes[name] = "private"
        env.manager._server_tenants[name] = 2
        env.manager._server_owners[name] = 7

        with pytest.raises(MCPResetBlockedError):
            await env.manager.reset_server(name, timeout=5)

        assert env.built == []
        logger.info("test_unsafe_managed_config_is_not_connected 完成")

    # 方法作用：验证内置 YAML 服务停用后拒绝重置且不建连。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_disabled_builtin_config_is_blocked(self, monkeypatch) -> None:
        """enabled: false 的内置配置不得被 reset 重新拉起。"""
        logger.debug("test_disabled_builtin_config_is_blocked 入口")
        from src.mcp_client.client_manager import MCPResetBlockedError

        env = ResetEnv(monkeypatch)
        name = "builtin_off"
        env.manager._configured_system_servers = {
            name: {"transport": "sse", "url": f"https://{name}.example.com/sse",
                   "enabled": False},
        }

        with pytest.raises(MCPResetBlockedError):
            await env.manager.reset_server(name, timeout=5)

        assert env.built == []
        logger.info("test_disabled_builtin_config_is_blocked 完成")

    # 方法作用：验证未知服务重置失败且不创建连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unknown_server_is_unavailable(self, monkeypatch) -> None:
        """未注册名称必须返回不可用而不是新建连接。"""
        logger.debug("test_unknown_server_is_unavailable 入口")
        from src.mcp_client.client_manager import MCPResetUnavailableError

        env = ResetEnv(monkeypatch)

        with pytest.raises(MCPResetUnavailableError):
            await env.manager.reset_server("private_2_7_missing", timeout=5)

        assert env.built == []
        logger.info("test_unknown_server_is_unavailable 完成")


class TestMCPResetBudget:
    """覆盖 spec/56 §5.5：总预算覆盖锁等待、拆旧与重建，取消后必须可重试。"""

    # 方法作用：验证同服务持锁期间到达的 reset 在总预算内超时。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_lock_wait_counts_against_total_budget(self, monkeypatch) -> None:
        """排队等待服务锁必须计入总预算，不能无限等待。"""
        logger.debug("test_lock_wait_counts_against_total_budget 入口")
        from src.mcp_client.client_manager import MCPResetTimeoutError

        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.manager._server_configs[name] = env.config(name)
        gate = env.manager._gates.lock(name)
        await gate.acquire()
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with pytest.raises(MCPResetTimeoutError):
                await env.manager.reset_server(name, timeout=0.05)
            elapsed = loop.time() - started
        finally:
            gate.release()

        assert elapsed < 1.0
        assert env.built == []
        # 超时不得误释放他人持有的锁，也不得留下连接：随后 reset 必须能正常完成。
        env.queue(name, FakeTransport(), ["echo"])
        result = await asyncio.wait_for(env.manager.reset_server(name, timeout=5), 5)
        assert result["tools"] == 1
        logger.info("test_lock_wait_counts_against_total_budget 完成")

    # 方法作用：验证旧连接拆除耗时计入总预算且不残留旧会话与工具。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_teardown_time_counts_against_total_budget(self, monkeypatch) -> None:
        """拆旧超过预算时必须在预算内超时退出，并保留可重试状态。"""
        logger.debug("test_teardown_time_counts_against_total_budget 入口")
        from src.mcp_client.client_manager import MCPResetTimeoutError

        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        # 旧 transport 退出缓慢：拆除旧连接会超过整个重置预算。
        env.queue(name, FakeTransport(exit_delay=3), ["echo"])
        await env.connect(name)

        with pytest.raises(MCPResetTimeoutError):
            await asyncio.wait_for(env.manager.reset_server(name, timeout=0.1), 5)

        assert name not in env.manager.sessions
        assert env.tool_keys(name) == []
        assert env.sessions[name][0].closed is True
        env.queue(name, FakeTransport(), ["echo"])
        result = await asyncio.wait_for(env.manager.reset_server(name, timeout=5), 5)

        assert result["tools"] == 1
        logger.info("test_teardown_time_counts_against_total_budget 完成")

    # 方法作用：验证取消 reset 后服务锁被释放且随后重试可成功。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_cancelled_reset_releases_lock_and_retries(self, monkeypatch) -> None:
        """取消必须释放服务锁；再次 reset 不得因锁泄漏而排队挂起。"""
        logger.debug("test_cancelled_reset_releases_lock_and_retries 入口")
        env = ResetEnv(monkeypatch)
        name = "private_2_7_docs"
        env.queue(name, FakeTransport(), ["echo"])
        await env.connect(name)
        gate = asyncio.Event()
        blocked = FakeTransport(enter_gate=gate)
        env.queue(name, blocked, ["echo"])

        task = asyncio.create_task(env.manager.reset_server(name, timeout=30))
        await wait_until(lambda: env.built.count(name) == 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        gate.set()
        await wait_until(lambda: blocked.enter_task is not None and blocked.enter_task.done())
        env.queue(name, FakeTransport(), ["echo"])
        # 取消路径若泄漏服务锁，这里会排队超时而不是完成重建。
        result = await asyncio.wait_for(env.manager.reset_server(name, timeout=5), 5)

        assert result["tools"] == 1
        assert env.manager.sessions[name] is env.manager._connections[name]
        assert env.sessions[name][-1].closed is False
        logger.info("test_cancelled_reset_releases_lock_and_retries 完成")


class TestMCPManagerCloseContract:
    """覆盖 spec/56 §4：manager 级跨任务关闭契约。"""

    # 方法作用：验证 close_all 从其他任务关闭所有 owner 连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_close_all_from_foreign_task_closes_connections(self, monkeypatch) -> None:
        """manager 级关闭不得跨任务退出任何栈。"""
        logger.debug("test_close_all_from_foreign_task_closes_connections 入口")
        env = ResetEnv(monkeypatch)
        for name in ("private_2_7_alpha", "private_2_7_beta"):
            env.queue(name, FakeTransport(), ["echo"])
            await env.connect(name)
        connections = dict(env.manager._connections)

        await asyncio.create_task(env.manager.close_all())

        assert env.manager.sessions == {}
        assert env.manager.langchain_tools == {}
        assert env.manager._connections == {}
        assert all(
            session.closed
            for sessions in env.sessions.values()
            for session in sessions
        )
        assert all(
            connection.owner_task is not None and connection.owner_task.done()
            for connection in connections.values()
        )
        logger.info("test_close_all_from_foreign_task_closes_connections 完成")
