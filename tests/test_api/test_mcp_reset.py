"""MCP reset API：授权、安全阻断、状态码映射与响应契约。

覆盖 11.1.12 / spec/56 §6：身份一律取自认证上下文；未知或不可见 404、
无权/停用/安全阻断 403 且不建立连接、存储不可用 503、超时 504、
其他失败脱敏 500；内置 YAML 服务仅平台超级管理员可重置。
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.auth import use_identity_context
from src.mcp_client.client_manager import (
    MCPResetBlockedError,
    MCPResetError,
    MCPResetUnavailableError,
)
from src.security.tenant_policy import RequestIdentity

logger = logging.getLogger(__name__)


# 方法作用：构造按真实 exc_info 形态渲染堆栈的日志替身，用于断言脱敏与单次计数。
# Args: errors - 收集已渲染 error 日志文本的列表。
# Returns: 与 reset 路由异常边界方法兼容的日志替身。
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
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )


# 方法作用：构造按 SQL 可见性结果返回的假数据库连接。
# Args: row - fetchrow 返回值；blocked - fetchval 返回值；fail - 为真时查询抛异常。
# Returns: 带 fetchrow/fetchval 的假连接。
def make_db(row: Any, blocked: bool = False, *, fail: bool = False) -> SimpleNamespace:
    if fail:
        return SimpleNamespace(
            fetchrow=AsyncMock(side_effect=RuntimeError("数据库不可用")),
            fetchval=AsyncMock(side_effect=RuntimeError("数据库不可用")),
        )
    return SimpleNamespace(
        fetchrow=AsyncMock(return_value=row),
        fetchval=AsyncMock(return_value=blocked),
    )


# 方法作用：构造数据库行映射（字段与 mcp_servers 查询列一致）。
# Args: scope - 作用域；tenant_id - 资源租户；owner_user_id - 所有者；
#       enabled - 是否启用；name - 公开名（仅用于断言可读性）。
# Returns: 行映射。
def make_row(
    scope: str = "private",
    tenant_id: int = 2,
    owner_user_id: int = 7,
    *,
    enabled: bool = True,
    name: str = "docs",
) -> dict[str, Any]:
    return {
        "id": 1, "name": name, "scope": scope, "tenant_id": tenant_id,
        "owner_user_id": owner_user_id, "transport": "sse",
        "url": f"https://{name}.example.com/sse", "enabled": enabled,
        "is_builtin": False,
    }


class ResetApiEnv:
    """装配路由级 reset 测试环境：假 manager、假身份与假数据库。"""

    # 方法作用：装配身份、数据库与 manager 替身。
    # Args: monkeypatch - pytest 替身工具；row - 数据库行；blocked - 安全阻断；
    #       fail_db - 数据库查询失败；resolved - manager 解析结果；
    #       builtin - 是否为内置 YAML 服务；reset_result - reset_server 返回；
    #       reset_error - reset_server 抛出的异常；prepare_error - 目标准备抛出的异常。
    # Returns: 无返回值。
    def __init__(
        self,
        monkeypatch: Any,
        *,
        row: Any = None,
        blocked: bool = False,
        fail_db: bool = False,
        resolved: tuple[str, str] | None = None,
        builtin: bool = False,
        reset_result: dict | None = None,
        reset_error: BaseException | None = None,
        prepare_error: BaseException | None = None,
    ) -> None:
        import src.api.auth as auth
        import src.api.routes as routes_pkg
        import src.mcp_client.client_manager as mcp_module

        monkeypatch.setattr(auth, "get_current_role", lambda: "tenant_admin")
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 2)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)

        connection = make_db(row, blocked, fail=fail_db)

        # 方法作用：提供带作用域身份注入的假数据库连接。
        # Args: 无。
        # Returns: 事务范围上下文。
        @asynccontextmanager
        async def fake_scoped_db():
            yield connection

        monkeypatch.setattr(routes_pkg, "_connect_scoped_mcp_db", fake_scoped_db)

        if reset_error is not None:
            reset_mock = AsyncMock(side_effect=reset_error)
        else:
            reset_mock = AsyncMock(
                return_value=dict(reset_result or {"scope": "private", "tools": 2}),
            )
        self.reset_mock = reset_mock
        if prepare_error is not None:
            self.prepare_mock = AsyncMock(side_effect=prepare_error)
        else:
            self.prepare_mock = AsyncMock()
        self.manager = SimpleNamespace(
            ensure_scoped_servers=AsyncMock(return_value=0),
            prepare_authorized_server=self.prepare_mock,
            resolve_resettable_server=lambda name, scope=None: resolved,
            is_builtin_server=lambda name: builtin,
            scoped_server_name=lambda name, scope, tenant, owner: (
                f"{scope}_{tenant}_{owner}_{name}"
            ),
            reset_server=reset_mock,
        )
        monkeypatch.setattr(mcp_module, "get_mcp_client_manager", lambda: self.manager)


class TestMcpResetApi:
    """覆盖 11.1.12：reset 路由的授权、安全与状态码契约。"""

    # 方法作用：验证成功重置使用当前身份与已授权配置并返回契约响应。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_success_returns_contract_and_authorized_config(self, monkeypatch) -> None:
        """身份一律取自认证上下文，受管配置由路由预授权后传给 manager。"""
        # Arrange
        import src.api.routes as routes

        row = make_row()
        env = ResetApiEnv(
            monkeypatch, row=row, resolved=("private_2_7_docs", "private"),
            reset_result={"scope": "private", "tools": 2},
        )

        # Act
        response = await routes.reset_mcp_server("docs")

        # Assert
        assert response == {"status": "ok", "name": "docs", "scope": "private", "tools": 2}
        env.reset_mock.assert_awaited_once()
        call = env.reset_mock.await_args
        assert call.args[0] == "private_2_7_docs"
        assert call.kwargs["authorized_config"] == {
            "transport": "sse", "url": "https://docs.example.com/sse",
        }
        # 授权先于连接：成功路径也只准备已授权目标，不按租户全量加载。
        env.manager.ensure_scoped_servers.assert_not_awaited()
        env.prepare_mock.assert_awaited_once()
        prepare_call = env.prepare_mock.await_args
        assert prepare_call.args[0] == "private_2_7_docs"
        assert prepare_call.kwargs == {
            "config": {"transport": "sse", "url": "https://docs.example.com/sse"},
            "scope": "private",
            "tenant_id": 2,
            "owner_user_id": 7,
        }

    # 方法作用：验证未知或对当前身份不可见的名称返回 404。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unknown_or_invisible_server_is_404(self, monkeypatch) -> None:
        """跨租户/已删除/未知名称统一 404，且不触发重置。"""
        # Arrange
        import src.api.routes as routes

        env = ResetApiEnv(monkeypatch, row=None, resolved=None)

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 404
        assert env.reset_mock.await_count == 0
        env.prepare_mock.assert_not_awaited()

    # 方法作用：验证可见但无管理权的身份返回 403 且不建立连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_non_admin_is_403_without_connect(self, monkeypatch) -> None:
        """租户普通成员不能重置租户公共 MCP。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes

        row = make_row(scope="tenant", owner_user_id=0)
        env = ResetApiEnv(
            monkeypatch, row=row, resolved=("tenant_2_0_docs", "tenant"),
        )
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 403
        assert env.reset_mock.await_count == 0
        env.prepare_mock.assert_not_awaited()
        env.manager.ensure_scoped_servers.assert_not_awaited()
        env.prepare_mock.assert_not_awaited()

    # 方法作用：验证安全阻断或角色不足时先 403，且零准备、零连接、零重建。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；
    #       blocked - 是否安全阻断；role - 当前角色。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("blocked,role", [(True, "tenant_admin"), (False, "analyst")])
    async def test_rejected_reset_never_prepares_or_loads(
        self, monkeypatch, blocked: bool, role: str,
    ) -> None:
        """授权检查必须早于任何连接：拒绝路径不得 prepare、加载或重建。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes

        row = make_row(scope="tenant", owner_user_id=0)
        env = ResetApiEnv(
            monkeypatch, row=row, blocked=blocked,
            resolved=("tenant_2_0_docs", "tenant"),
        )
        monkeypatch.setattr(auth, "get_current_role", lambda: role)

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 403
        env.manager.ensure_scoped_servers.assert_not_awaited()
        env.prepare_mock.assert_not_awaited()
        env.reset_mock.assert_not_awaited()

    # 方法作用：验证数据库合法但尚未加载的目标可以正常重置。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unloaded_visible_server_is_prepared_then_reset(self, monkeypatch) -> None:
        """未加载目标只准备该目标，禁止用整租户加载实现 reset。"""
        # Arrange
        import src.api.routes as routes

        row = make_row()
        env = ResetApiEnv(
            monkeypatch, row=row, resolved=None,
            reset_result={"scope": "private", "tools": 1},
        )

        # Act
        response = await routes.reset_mcp_server("docs")

        # Assert
        assert response == {"status": "ok", "name": "docs", "scope": "private", "tools": 1}
        env.manager.ensure_scoped_servers.assert_not_awaited()
        env.prepare_mock.assert_awaited_once()
        prepare_call = env.prepare_mock.await_args
        assert prepare_call.args[0] == "private_2_7_docs"
        assert prepare_call.kwargs == {
            "config": {"transport": "sse", "url": "https://docs.example.com/sse"},
            "scope": "private",
            "tenant_id": 2,
            "owner_user_id": 7,
        }
        env.reset_mock.assert_awaited_once()

    # 方法作用：验证停用配置返回 403 且不建立连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_disabled_server_is_403_without_connect(self, monkeypatch) -> None:
        """enabled=false 的服务不得被 reset 绕过重启。"""
        # Arrange
        import src.api.routes as routes

        row = make_row(enabled=False)
        env = ResetApiEnv(
            monkeypatch, row=row, resolved=("private_2_7_docs", "private"),
        )

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 403
        assert env.reset_mock.await_count == 0
        env.prepare_mock.assert_not_awaited()

    # 方法作用：验证安全阻断的服务返回 403 且不建立连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_security_blocked_server_is_403_without_connect(self, monkeypatch) -> None:
        """mcp_security_blocks.blocked=true 时禁止重建连接。"""
        # Arrange
        import src.api.routes as routes

        row = make_row()
        env = ResetApiEnv(
            monkeypatch, row=row, blocked=True,
            resolved=("private_2_7_docs", "private"),
        )

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 403
        assert env.reset_mock.await_count == 0
        env.prepare_mock.assert_not_awaited()

    # 方法作用：验证内置 YAML 服务仅平台超级管理员可重置。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_builtin_requires_platform_super_admin(self, monkeypatch) -> None:
        """租户管理员不能重置不在数据库中的内置 system 服务。"""
        # Arrange
        import src.api.routes as routes

        env = ResetApiEnv(
            monkeypatch, row=None, resolved=("builtin_docs", "system"), builtin=True,
        )

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("builtin_docs")
        assert caught.value.status_code == 403
        assert env.reset_mock.await_count == 0
        env.prepare_mock.assert_not_awaited()

    # 方法作用：验证平台超级管理员可重置内置服务且不传数据库配置。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_builtin_reset_by_super_admin(self, monkeypatch) -> None:
        """内置服务使用 YAML 配置，authorized_config 必须为 None。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes

        env = ResetApiEnv(
            monkeypatch, row=None, resolved=("builtin_docs", "system"), builtin=True,
            reset_result={"scope": "system", "tools": 1},
        )
        monkeypatch.setattr(auth, "get_current_role", lambda: "super_admin")

        # Act
        response = await routes.reset_mcp_server("builtin_docs")

        # Assert
        assert response["tools"] == 1
        assert env.reset_mock.await_args.kwargs["authorized_config"] is None

    # 方法作用：验证数据库已无该行但运行时仍保留配置时返回 404。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_deleted_row_does_not_resurrect(self, monkeypatch) -> None:
        """删除为终态：即使运行时残留配置也不得重建。"""
        # Arrange
        import src.api.routes as routes

        env = ResetApiEnv(
            monkeypatch, row=None, resolved=("private_2_7_docs", "private"),
        )

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 404
        assert env.reset_mock.await_count == 0
        env.prepare_mock.assert_not_awaited()

    # 方法作用：验证作用域消歧与数据库行不一致时按不可见返回 404。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_scope_mismatch_is_404(self, monkeypatch) -> None:
        """数据库行作用域与运行时解析结果不一致时不得重置。"""
        # Arrange
        import src.api.routes as routes

        row = make_row(scope="private")
        env = ResetApiEnv(
            monkeypatch, row=row, resolved=("tenant_2_0_docs", "tenant"),
        )

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 404
        assert env.reset_mock.await_count == 0
        env.prepare_mock.assert_not_awaited()

    # 方法作用：验证非法 scope 参数在查库前即返回 400。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_invalid_scope_is_400(self, monkeypatch) -> None:
        """非法作用域不得进入授权或连接流程。"""
        # Arrange
        import src.api.routes as routes

        env = ResetApiEnv(monkeypatch, row=None)

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs", scope="shared")
        assert caught.value.status_code == 400
        assert env.reset_mock.await_count == 0

    # 方法作用：验证授权阶段查库失败返回 503 且不建立连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_storage_unavailable_is_503(self, monkeypatch) -> None:
        """配置存储不可用属于基础设施失败，错误文案不含内部细节。"""
        # Arrange
        import src.api.routes as routes

        env = ResetApiEnv(monkeypatch, fail_db=True, resolved=None)

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")
        assert caught.value.status_code == 503
        assert caught.value.detail == "MCP Server 配置存储不可用"
        assert env.reset_mock.await_count == 0

    # 方法作用：验证 manager 的各类失败按契约映射为 404/403/504/500。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_manager_failures_map_to_contract_status(self, monkeypatch) -> None:
        """超时 504、不可用 404、阻断 403、未分类 500 且文案脱敏。"""
        # Arrange
        import src.api.routes as routes
        from src.mcp_client.client_manager import (
            MCPResetBlockedError,
            MCPResetError,
            MCPResetTimeoutError,
            MCPResetUnavailableError,
        )

        row = make_row()
        cases = (
            (MCPResetTimeoutError("内部超时细节"), 504),
            (MCPResetUnavailableError("内部删除细节"), 404),
            (MCPResetBlockedError("https://internal.example.com/sse 被拒绝"), 403),
            (MCPResetError("内部栈信息"), 500),
            (RuntimeError("内部连接串 postgres://secret"), 500),
        )
        for error, expected_status in cases:
            env = ResetApiEnv(
                monkeypatch, row=row, resolved=("private_2_7_docs", "private"),
                reset_error=error,
            )

            # Act / Assert
            with pytest.raises(HTTPException) as caught:
                await routes.reset_mcp_server("docs")
            assert caught.value.status_code == expected_status
            assert "http" not in caught.value.detail
            assert "postgres" not in caught.value.detail
            assert "内部" not in caught.value.detail
            assert env.reset_mock.await_count == 1

    # 方法作用：验证 reset 路由按契约路径注册在领域路由器上。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reset_route_is_registered(self) -> None:
        """POST /mcp/{name}/reset 必须存在于导出路由表中。"""
        # Arrange / Act
        from src.api.routes import router

        matches = [
            route for route in router.routes
            if getattr(route, "path", "") == "/mcp/{name}/reset"
            and "POST" in getattr(route, "methods", set())
        ]

        # Assert
        assert len(matches) == 1

    # 方法作用：验证 super_admin 只能解析 system 作用域的可重置服务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_manager_resolver_limits_super_admin_to_system(self, monkeypatch) -> None:
        """平台超管不得通过 reset 解析租户或私有服务。"""
        # Arrange
        import src.api.auth as auth
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        manager._server_configs["tenant_2_0_docs"] = {
            "transport": "sse", "url": "https://x/sse",
        }
        manager._server_scopes["tenant_2_0_docs"] = "tenant"
        manager._server_tenants["tenant_2_0_docs"] = 2
        manager._server_owners["tenant_2_0_docs"] = 0
        manager._configured_system_servers["builtin"] = {
            "transport": "sse", "url": "https://y/sse",
        }
        monkeypatch.setattr(auth, "get_current_role", lambda: "super_admin")
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 2)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)

        # Act / Assert
        assert manager.resolve_resettable_server("docs") is None
        assert manager.resolve_resettable_server("builtin") == ("builtin", "system")


# 方法作用：装配真实 manager 的路由环境：假身份与假数据库，真实服务锁。
# Args: monkeypatch - pytest 替身工具；row - 数据库行；role - 当前角色。
# Returns: (manager, internal_name)；调用方需自行接管 reset_server 以避免真实连接。
def make_real_manager_env(
    monkeypatch: Any,
    row: Any,
    *,
    role: str = "tenant_admin",
) -> tuple[Any, str]:
    import src.api.auth as auth
    import src.api.routes as routes_pkg
    import src.mcp_client.client_manager as mcp_module
    from src.mcp_client.client_manager import MCPClientManager

    connection = make_db(row, False)

    # 方法作用：提供带作用域身份注入的假数据库连接。
    # Args: 无。
    # Returns: 事务范围上下文。
    @asynccontextmanager
    async def fake_scoped_db():
        yield connection

    manager = MCPClientManager()
    monkeypatch.setattr(auth, "get_current_role", lambda: role)
    monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
    monkeypatch.setattr(auth, "get_current_user_id", lambda: 9)
    monkeypatch.setattr(routes_pkg, "_connect_scoped_mcp_db", fake_scoped_db)
    monkeypatch.setattr(mcp_module, "get_mcp_client_manager", lambda: manager)
    internal_name = manager.scoped_server_name(
        str(row["name"]),
        str(row["scope"]),
        int(row["tenant_id"] or 0),
        int(row["owner_user_id"] or 0),
    )
    return manager, internal_name


class TestMcpResetRouteDeadline:
    """覆盖 11.1.12 / spec/56 §5.5：目标准备、锁等待与重建共享同一总预算。"""

    # 方法作用：验证冷目标（尚未加载）持锁时路由在预算内以 504 结束。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_cold_target_lock_wait_is_bounded_to_504(self, monkeypatch) -> None:
        """目标未加载且同名操作持锁时，准备阶段也必须在预算内超时。"""
        import src.api.routes.mcp as mcp_routes

        row = make_row(scope="tenant", tenant_id=7, owner_user_id=0, name="shared")
        manager, internal_name = make_real_manager_env(monkeypatch, row)
        manager.reset_server = AsyncMock(return_value={"scope": "tenant", "tools": 1})
        gate = manager._gates.lock(internal_name)
        await gate.acquire()
        monkeypatch.setattr(mcp_routes, "_MCP_RESET_TIMEOUT_SECONDS", 0.02)

        request = asyncio.create_task(mcp_routes.reset_mcp_server("shared", "tenant"))
        try:
            done, _pending = await asyncio.wait({request}, timeout=0.3)

            assert request in done, "目标准备不能在 reset 计时之前无限等待服务锁"
            with pytest.raises(HTTPException) as caught:
                await request
            assert caught.value.status_code == 504
            # 超时取消不得误释放他人持有的锁，也不得触达重建。
            assert gate.locked() is True
            manager.reset_server.assert_not_awaited()
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            gate.release()

    # 方法作用：验证既有目标（配置已加载）持锁时锁等待同样计入总预算。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_loaded_target_lock_wait_is_bounded_to_504(self, monkeypatch) -> None:
        """degraded/unavailable 目标保留配置，被同名操作持锁时同样按预算 504。"""
        import src.api.routes.mcp as mcp_routes

        row = make_row(scope="tenant", tenant_id=7, owner_user_id=0, name="shared")
        manager, internal_name = make_real_manager_env(monkeypatch, row)
        manager.reset_server = AsyncMock(return_value={"scope": "tenant", "tools": 1})
        manager._server_configs[internal_name] = {
            "transport": "sse", "url": "https://shared.example.com/sse",
        }
        manager._server_scopes[internal_name] = "tenant"
        manager._server_tenants[internal_name] = 7
        manager._server_owners[internal_name] = 0
        gate = manager._gates.lock(internal_name)
        await gate.acquire()
        monkeypatch.setattr(mcp_routes, "_MCP_RESET_TIMEOUT_SECONDS", 0.02)

        request = asyncio.create_task(mcp_routes.reset_mcp_server("shared", "tenant"))
        try:
            done, _pending = await asyncio.wait({request}, timeout=0.3)

            assert request in done, "既有目标的锁等待不能绕过总预算"
            with pytest.raises(HTTPException) as caught:
                await request
            assert caught.value.status_code == 504
            assert gate.locked() is True
            manager.reset_server.assert_not_awaited()
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            gate.release()

    # 方法作用：验证目标准备失败与重置失败走同一套脱敏状态码映射。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；
    #       error - 准备阶段抛出的异常；expected_status - 期望 HTTP 状态码。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "error,expected_status",
        [
            (MCPResetUnavailableError("内部删除细节"), 404),
            (MCPResetBlockedError("https://internal.example.com/sse 被拒绝"), 403),
            (MCPResetError("内部栈信息"), 500),
            (RuntimeError("内部连接串 postgres://secret"), 500),
        ],
    )
    async def test_prepare_failure_maps_like_reset(
        self, monkeypatch, error: BaseException, expected_status: int,
    ) -> None:
        """准备异常不得漏成未处理异常：404/403/500 且文案脱敏、不继续重建。"""
        import src.api.routes as routes

        env = ResetApiEnv(
            monkeypatch,
            row=make_row(),
            resolved=("private_2_7_docs", "private"),
            prepare_error=error,
        )

        with pytest.raises(HTTPException) as caught:
            await routes.reset_mcp_server("docs")

        assert caught.value.status_code == expected_status
        assert "http" not in caught.value.detail
        assert "postgres" not in caught.value.detail
        assert "内部" not in caught.value.detail
        env.reset_mock.assert_not_awaited()

    # 方法作用：验证目标准备耗时从重置剩余预算中扣除，而非重置重新获得完整预算。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prepare_time_is_deducted_from_reset_budget(self, monkeypatch) -> None:
        """总 deadline 从进入准备开始计时：reset_server 只能拿到剩余预算。"""
        import src.api.routes as routes
        import src.api.routes.mcp as mcp_routes

        # 方法作用：模拟耗时准备，占掉总预算的一部分。
        # Args: args/kwargs - 透传路由传入的准备参数。
        # Returns: 无返回值。
        async def slow_prepare(*args, **kwargs):
            await asyncio.sleep(0.05)

        env = ResetApiEnv(
            monkeypatch, row=make_row(), resolved=("private_2_7_docs", "private"),
        )
        env.prepare_mock = AsyncMock(side_effect=slow_prepare)
        env.manager.prepare_authorized_server = env.prepare_mock
        monkeypatch.setattr(mcp_routes, "_MCP_RESET_TIMEOUT_SECONDS", 0.5)

        response = await routes.reset_mcp_server("docs")

        assert response["status"] == "ok"
        remaining = env.reset_mock.await_args.kwargs["timeout"]
        # 准备至少消耗 50ms：不能把完整预算原样传给重建。
        assert 0 < remaining <= 0.47

    # 方法作用：验证取消路由等待后服务锁未被破坏，后续请求可完成准备与重建。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_cancelled_request_leaves_lock_retryable(self, monkeypatch) -> None:
        """取消传播后锁不泄漏：重试请求能正常准备目标并返回契约响应。"""
        import src.api.routes.mcp as mcp_routes

        row = make_row(scope="tenant", tenant_id=7, owner_user_id=0, name="shared")
        manager, internal_name = make_real_manager_env(monkeypatch, row)
        reset_mock = AsyncMock(return_value={"scope": "tenant", "tools": 1})
        manager.reset_server = reset_mock
        gate = manager._gates.lock(internal_name)
        await gate.acquire()

        request = asyncio.create_task(mcp_routes.reset_mcp_server("shared", "tenant"))
        await asyncio.sleep(0.1)
        assert not request.done(), "持锁期间请求必须处于等待而不是提前返回"
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        # 取消路径必须释放自己的等待而不动他人持有的锁。
        assert gate.locked() is True
        gate.release()

        response = await asyncio.wait_for(
            mcp_routes.reset_mcp_server("shared", "tenant"), 5,
        )

        assert response == {"status": "ok", "name": "shared", "scope": "tenant", "tools": 1}
        assert internal_name in manager._server_configs
        reset_mock.assert_awaited_once()

    # 方法作用：验证未授权角色在准备之前被 403 拒绝，真实 manager 零副作用。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unauthorized_role_never_prepares_real_manager(self, monkeypatch) -> None:
        """analyst 无管理权：403 且真实 manager 未登记任何目标、未建立连接。"""
        import src.api.routes.mcp as mcp_routes

        row = make_row(scope="tenant", tenant_id=7, owner_user_id=0, name="shared")
        manager, internal_name = make_real_manager_env(monkeypatch, row, role="analyst")
        prepare_spy = AsyncMock(side_effect=manager.prepare_authorized_server)
        manager.prepare_authorized_server = prepare_spy

        with pytest.raises(HTTPException) as caught:
            await mcp_routes.reset_mcp_server("shared", "tenant")

        assert caught.value.status_code == 403
        prepare_spy.assert_not_awaited()
        assert manager._server_configs == {}
        assert manager.sessions == {}
        assert not manager._gates.lock(internal_name).locked()


class TestMcpResetLoggingBoundary:
    """覆盖 11.1.12：异常边界脱敏为固定文案 + 原堆栈 + error_type，且只记录一次。"""

    # 方法作用：放行本类使用的受管 SSE 主机，避免真实 DNS 解析影响异常路径断言。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    def _allow_test_host(self, monkeypatch) -> None:
        import src.config as config_module

        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(mcp_remote_host_allowlist="shared.example.com"),
        )
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )

    # 方法作用：验证真实连接失败穿过 owner→manager→route 时只记录一次脱敏 error。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_route_reset_failure_logs_once_without_connection_secret(
        self, monkeypatch,
    ) -> None:
        """transport 携带连接凭证失败：500 固定文案，日志不含凭证原文。"""
        import src.api.routes.mcp as mcp_routes
        import src.mcp_client.client_manager as managers
        import src.mcp_client.owner_task as owner
        from tests.test_mcp.lifecycle_fakes import FakeTransport

        secret = "SYNTHETIC-MCP-CONNECT-CREDENTIAL"
        row = make_row(scope="tenant", tenant_id=7, owner_user_id=0, name="shared")
        manager, internal_name = make_real_manager_env(monkeypatch, row)
        self._allow_test_host(monkeypatch)
        monkeypatch.setattr(
            manager,
            "_build_transport_context",
            lambda config: FakeTransport(enter_error=RuntimeError(secret)),
        )
        # 同一异常真实穿过三层：必须联合捕获 owner/manager/route 日志，
        # 只 patch 路由单层会漏检跨层重复落盘。
        errors: list[str] = []
        capture = capturing_logger(errors)
        for module in (mcp_routes, managers, owner):
            monkeypatch.setattr(module, "logger", capture)

        with use_identity_context(RequestIdentity(tenant_id=7, user_id=9, role="tenant_admin")):
            with pytest.raises(HTTPException) as failure:
                await mcp_routes.reset_mcp_server("shared", "tenant")

        assert failure.value.status_code == 500
        assert failure.value.detail == "重置 MCP Server 失败"
        assert secret not in str(failure.value.detail)
        # 单次记录归属 owner 处理边界：路由不得重复写同一堆栈，也不得吞掉记录。
        assert len(errors) == 1, errors
        assert "MCP owner 连接异常结束" in errors[0]
        assert "重置 MCP Server API 失败" not in errors[0]
        assert secret not in errors[0]
        assert "RuntimeError" in errors[0]
        assert "lifecycle_fakes" in errors[0]
        # 失败后可重试：目标配置保留、无会话残留、锁已释放。
        assert internal_name in manager._server_configs
        assert manager.sessions == {}
        assert not manager._gates.lock(internal_name).locked()

    # 方法作用：验证 owner 尚未介入的构建失败仍由路由边界单次脱敏记录。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_route_owned_build_failure_logs_once_without_owner_record(
        self, monkeypatch,
    ) -> None:
        """连接构建阶段直接异常（owner 未启动）：跨层去重不得误伤路由记录。"""
        import src.api.routes.mcp as mcp_routes
        import src.mcp_client.client_manager as managers
        import src.mcp_client.owner_task as owner

        secret = "SYNTHETIC-MCP-BUILD-CREDENTIAL"

        # 方法作用：模拟 transport 构建阶段抛出携带敏感正文的未记录异常。
        # Args: config - 路由已授权的受管配置。
        # Returns: 不返回，始终抛出。
        def failing_build(config):
            raise ValueError(secret)

        row = make_row(scope="tenant", tenant_id=7, owner_user_id=0, name="shared")
        manager, internal_name = make_real_manager_env(monkeypatch, row)
        self._allow_test_host(monkeypatch)
        monkeypatch.setattr(manager, "_build_transport_context", failing_build)
        errors: list[str] = []
        capture = capturing_logger(errors)
        for module in (mcp_routes, managers, owner):
            monkeypatch.setattr(module, "logger", capture)

        with use_identity_context(RequestIdentity(tenant_id=7, user_id=9, role="tenant_admin")):
            with pytest.raises(HTTPException) as failure:
                await mcp_routes.reset_mcp_server("shared", "tenant")

        assert failure.value.status_code == 500
        assert secret not in str(failure.value.detail)
        # owner 未记录该异常：路由必须单次落盘固定文案 + error_type + 原堆栈。
        assert len(errors) == 1, errors
        assert "重置 MCP Server API 失败" in errors[0]
        assert "ValueError" in errors[0]
        assert "failing_build" in errors[0]
        assert secret not in errors[0]
        assert internal_name in manager._server_configs
        assert manager.sessions == {}
        assert not manager._gates.lock(internal_name).locked()

    # 方法作用：验证路由自身准备阶段失败时同套脱敏记录与 500 映射仅发生一次。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_route_preparation_failure_logs_once_without_secret(
        self, monkeypatch,
    ) -> None:
        """目标准备抛出的敏感正文不得进入日志，运行时异常统一 500 固定文案。"""
        import src.api.routes.mcp as mcp_routes

        secret = "SYNTHETIC-MCP-RESET-CREDENTIAL"
        row = make_row(scope="tenant", tenant_id=7, owner_user_id=0, name="shared")
        manager, _internal_name = make_real_manager_env(monkeypatch, row)
        self._allow_test_host(monkeypatch)

        # 方法作用：模拟已授权目标准备阶段抛出携带敏感正文的异常。
        # Args: args/kwargs - 路由传入的准备参数。
        # Returns: 无返回值，始终抛出。
        async def sensitive_prepare(*args, **kwargs):
            raise RuntimeError(secret)

        manager.prepare_authorized_server = sensitive_prepare
        errors: list[str] = []
        monkeypatch.setattr(mcp_routes, "logger", capturing_logger(errors))

        with use_identity_context(RequestIdentity(tenant_id=7, user_id=9, role="tenant_admin")):
            with pytest.raises(HTTPException) as failure:
                await mcp_routes.reset_mcp_server("shared", "tenant")

        assert failure.value.status_code == 500
        assert secret not in str(failure.value.detail)
        assert len(errors) == 1, errors
        assert secret not in errors[0]
        assert "RuntimeError" in errors[0]
        # 原始堆栈必须保留抛错位置：sensitive_prepare 抛出、reset_mcp_server 捕获；
        # 异常在路由内被捕获，外层测试函数帧不属于该异常堆栈。
        assert "sensitive_prepare" in errors[0]
        assert "reset_mcp_server" in errors[0]
