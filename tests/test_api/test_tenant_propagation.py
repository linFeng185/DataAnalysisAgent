"""API 路由租户参数传递回归测试。"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.schemas import AutomationScheduleCreateRequest, ChatRequest


@pytest.mark.asyncio
class TestApiTenantPropagation:
    """覆盖多租户 API 读取数据源时的身份边界。"""

    # 方法作用：验证 Chat 授权候选读取显式携带当前租户。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chat_access_passes_current_tenant_to_registry(self, monkeypatch) -> None:
        """Chat 不能用默认租户的数据源候选覆盖当前租户。"""
        # Arrange
        import src.api.auth as auth_module
        import src.api.routes as routes_module
        import src.app_context as app_context_module
        import src.security.permission_check as permission_module

        chat_module = importlib.import_module("src.api.routes.chat")
        calls: list[int | None] = []

        async def _list_all(*, tenant_id: int | None = None):
            calls.append(tenant_id)
            return [{"name": "sales", "dialect": "postgres"}]

        registry = SimpleNamespace(list_all=_list_all)
        policy = SimpleNamespace(datasource_isolation_enabled=True)
        monkeypatch.setattr(routes_module, "_registry", lambda: registry)
        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 42)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")
        monkeypatch.setattr(app_context_module, "get_tenant_policy", lambda: policy)
        monkeypatch.setattr(
            permission_module,
            "resolve_datasource_access",
            AsyncMock(return_value={"sales": {"name": "sales"}}),
        )

        # Act
        result = await chat_module._resolve_chat_access(
            ChatRequest(query="查询销售", datasource="sales"),
        )

        # Assert
        assert result["sales"]["name"] == "sales"
        assert calls == [42]

    # 方法作用：验证数据源分页列表读取显式携带当前租户。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_datasource_list_passes_current_tenant_to_registry(self, monkeypatch) -> None:
        """数据源管理列表不得先读取全局数据再依赖后置 ACL 过滤。"""
        # Arrange
        import src.api.auth as auth_module
        import src.api.routes as routes_module
        import src.app_context as app_context_module
        import src.security.permission_check as permission_module

        datasource_module = importlib.import_module("src.api.routes.datasource")
        calls: list[int | None] = []

        async def _list_all(*, tenant_id: int | None = None):
            calls.append(tenant_id)
            return [{"name": "sales", "dialect": "postgres"}]

        registry = SimpleNamespace(list_all=_list_all)
        policy = SimpleNamespace(datasource_isolation_enabled=True)
        monkeypatch.setattr(routes_module, "_registry", lambda: registry)
        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 42)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")
        monkeypatch.setattr(app_context_module, "get_tenant_policy", lambda: policy)
        monkeypatch.setattr(
            permission_module,
            "resolve_datasource_access",
            AsyncMock(return_value={"sales": {"name": "sales"}}),
        )

        # Act
        result = await datasource_module.list_datasources(page=1, page_size=20)

        # Assert
        assert result["total"] == 1
        assert calls == [42]

    # 方法作用：验证自动化任务创建校验的解析和授权列表共用当前租户。
    # Args: self - pytest 测试类实例；monkeypatch - Registry、身份策略和权限补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_automation_validation_passes_current_tenant_to_registry(self, monkeypatch) -> None:
        """自动化任务不能绑定其他租户同名数据源。"""
        # Arrange
        import src.api.routes.automation as automation_module
        import src.security.permission_check as permission_module

        calls: list[tuple[str, int | None]] = []

        async def _resolve(name: str, *, tenant_id: int | None = None):
            calls.append(("resolve", tenant_id))
            return SimpleNamespace(name=name, dialect="postgres")

        async def _list_all(*, tenant_id: int | None = None):
            calls.append(("list_all", tenant_id))
            return [{"name": "sales", "dialect": "postgres"}]

        registry = SimpleNamespace(resolve=_resolve, list_all=_list_all)
        policy = SimpleNamespace(datasource_isolation_enabled=True)
        identity = SimpleNamespace(tenant_id=42, user_id=7, role="tenant_admin")
        monkeypatch.setattr(automation_module, "get_registry", lambda: registry)
        monkeypatch.setattr(automation_module, "get_tenant_policy", lambda: policy)
        monkeypatch.setattr(
            permission_module,
            "resolve_datasource_access",
            AsyncMock(return_value={"sales": {"name": "sales"}}),
        )
        request = AutomationScheduleCreateRequest(
            name="daily-sales",
            kind="report",
            datasource="sales",
            sql="SELECT 1",
            frequency="daily",
        )

        # Act
        dialect = await automation_module._validate_schedule_request(identity, request)

        # Assert
        assert dialect == "postgres"
        assert calls == [("resolve", 42), ("list_all", 42)]

    # 方法作用：验证 Schema 路由解析和延迟内省都使用当前租户。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_routes_pass_current_tenant_to_registry_and_manager(self, monkeypatch) -> None:
        """Schema 路由不能因省略租户参数读取其他租户同名数据源。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.api.auth as auth_module
        import src.api.routes as routes_module
        import src.api.routes.schema as schema_module
        import src.app_context as app_context_module
        import src.security.permission_check as permission_module

        identity = SimpleNamespace(tenant_id=42, user_id=7, role="analyst")
        calls: list[tuple[str, int | None]] = []

        async def _list_all(*, tenant_id: int | None = None):
            calls.append(("list_all", tenant_id))
            return [{"name": "sales", "dialect": "postgres"}]

        async def _resolve(name: str, *, tenant_id: int | None = None):
            calls.append(("resolve", tenant_id))
            return SimpleNamespace(name=name, schema=None)

        registry = SimpleNamespace(list_all=_list_all, resolve=_resolve)
        manager = SimpleNamespace(
            get_or_fetch_schema=AsyncMock(return_value=SimpleNamespace(tables=[])),
        )
        monkeypatch.setattr(auth_module, "get_current_identity", lambda: identity)
        monkeypatch.setattr(routes_module, "_registry", lambda: registry)
        monkeypatch.setattr(routes_module, "_schema_manager", lambda: manager)
        monkeypatch.setattr(
            app_context_module,
            "get_tenant_policy",
            lambda: SimpleNamespace(datasource_isolation_enabled=True),
        )
        monkeypatch.setattr(
            permission_module,
            "resolve_datasource_access",
            AsyncMock(return_value={"sales": {"name": "sales"}}),
        )

        # Act
        result = await schema_module.list_tables(
            datasource="sales",
            page=1,
            page_size=20,
            search="",
        )

        # Assert
        assert result["datasource"] == "sales"
        assert calls == [("list_all", 42), ("resolve", 42)]
        manager.get_or_fetch_schema.assert_awaited_once_with("sales", tenant_id=42)

    # 方法作用：验证 SQL 优化建议获取 Schema 时保留当前租户边界。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_optimization_schema_fetch_passes_current_tenant(self, monkeypatch) -> None:
        """优化建议不得从默认租户缓存读取同名数据源 Schema。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.api.auth as auth_module
        import src.api.routes.optimization as optimization_module
        import src.app_context as app_context_module
        import src.datasource.registry as registry_module
        import src.knowledge.schema_manager as schema_manager_module
        import src.security.permission_check as permission_module

        identity = SimpleNamespace(tenant_id=42, user_id=7, role="analyst")
        datasource = SimpleNamespace(dialect="postgres", schema=None)
        registry = SimpleNamespace(
            list_all=AsyncMock(return_value=[{"name": "sales", "dialect": "postgres"}]),
            resolve=AsyncMock(return_value=datasource),
        )
        manager = SimpleNamespace(
            get_or_fetch_schema=AsyncMock(return_value=SimpleNamespace(tables=[])),
        )
        monkeypatch.setattr(auth_module, "get_current_identity", lambda: identity)
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        monkeypatch.setattr(schema_manager_module, "get_schema_manager", lambda: manager)
        monkeypatch.setattr(app_context_module, "get_tenant_policy", lambda: object())
        monkeypatch.setattr(
            permission_module,
            "resolve_datasource_access",
            AsyncMock(return_value={"sales": {}}),
        )

        # Act
        result = await optimization_module.advice({
            "sql": "SELECT id FROM orders",
            "dialect": "postgres",
            "datasource": "sales",
        })

        # Assert
        assert result["executed"] is False
        manager.get_or_fetch_schema.assert_awaited_once_with("sales", tenant_id=42)
