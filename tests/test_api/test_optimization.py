"""自动优化建议 API 测试。"""

from __future__ import annotations


class TestOptimizationRoute:
    """覆盖自动优化建议接口功能 19.10。"""

    # 方法作用：验证 API 仅返回确定性建议，不执行数据库操作。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值。
    async def test_advice_endpoint_is_non_mutating(self) -> None:
        """纯 SQL 输入应返回 executed=false。"""
        # Arrange
        from src.api.routes.optimization import advice

        # Act
        result = await advice({
            "sql": "SELECT id FROM orders WHERE customer_id = 1",
            "dialect": "postgres",
            "schema": {"orders": {"columns": ["id", "customer_id"]}},
        })

        # Assert
        assert result["executed"] is False
        assert result["valid"] is True

    # 方法作用：验证优化建议读取数据源时显式注入集中租户策略和身份。
    # Args: self - pytest 测试类实例；monkeypatch - 身份、Registry 与权限补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_advice_injects_central_tenant_policy(self, monkeypatch) -> None:
        """优化建议不能因漏传 tenant_policy 丢失行列权限。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.api.auth as auth_module
        import src.app_context as app_context_module
        import src.datasource.registry as registry_module
        import src.security.permission_check as permission_module
        from src.api.routes.optimization import advice

        identity = SimpleNamespace(tenant_id=4, user_id=7, role="analyst")
        datasource = SimpleNamespace(dialect="postgres", schema={"orders": {"columns": ["id"]}})
        registry = SimpleNamespace(
            list_all=AsyncMock(return_value=[{"name": "sales", "dialect": "postgres"}]),
            resolve=AsyncMock(return_value=datasource),
        )
        resolver = AsyncMock(return_value={
            "sales": {"allowed_columns": ["orders.id"], "row_filter_sql": "tenant_id = 4"},
        })
        policy = object()
        monkeypatch.setattr(auth_module, "get_current_identity", lambda: identity)
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        monkeypatch.setattr(permission_module, "resolve_datasource_access", resolver)
        monkeypatch.setattr(app_context_module, "get_tenant_policy", lambda: policy)

        # Act
        result = await advice({
            "sql": "SELECT orders.id FROM orders",
            "dialect": "postgres",
            "datasource": "sales",
        })

        # Assert
        assert result["executed"] is False
        assert resolver.await_args.kwargs["tenant_policy"] is policy
