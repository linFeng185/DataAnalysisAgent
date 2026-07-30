"""API 访问策略数据库迁移与平台管理接口测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

logger = logging.getLogger(__name__)


class TestAccessPolicyAdmin:
    """覆盖功能 22.3.1-22.3.3：动态策略和 IP 规则管理。"""

    # 方法作用：验证数据库迁移包含动态策略、CIDR 规则和必要约束。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_access_policy_migration_contract(self) -> None:
        """策略与 IP 规则必须由 PostgreSQL 约束非法枚举和重复项。"""
        logger.debug("test_access_policy_migration_contract 入口")
        sql = Path("migrations/007_api_access_policy.sql").read_text(encoding="utf-8")

        assert "CREATE TABLE IF NOT EXISTS api_access_policies" in sql
        assert "CREATE TABLE IF NOT EXISTS api_ip_rules" in sql
        assert "CIDR" in sql
        assert "auth_mode IN ('jwt', 'jwt_or_admin_key', 'super_admin')" in sql
        assert "UNIQUE (policy_key, action, cidr)" in sql
        logger.info("test_access_policy_migration_contract 完成")

    # 方法作用：验证访问策略管理路由完整注册。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_access_policy_admin_routes_are_registered(self) -> None:
        """策略和 IP 规则的列表、创建、更新、删除端点缺一不可。"""
        logger.debug("test_access_policy_admin_routes_are_registered 入口")
        from src.api.routes import router
        from src.api.routes.access_policy import router as access_policy_router

        mounted_routes = {
            (method, route.path)
            for route in router.routes
            for method in (getattr(route, "methods", None) or set())
        }
        routes = {
            (method, route.path)
            for route in access_policy_router.routes
            for method in (getattr(route, "methods", None) or set())
        }
        access_policy_included = routes.issubset(mounted_routes)
        logger.info(
            "访问策略路由注册探针",
            extra={
                "included": access_policy_included,
                "route_count": len(routes),
            },
        )

        assert access_policy_included is True
        assert ("GET", "/admin/access-policies") in routes
        assert ("POST", "/admin/access-policies") in routes
        assert ("PATCH", "/admin/access-policies/{policy_id}") in routes
        assert ("DELETE", "/admin/access-policies/{policy_id}") in routes
        assert ("POST", "/admin/access-policies/{policy_key}/ip-rules") in routes
        assert ("PATCH", "/admin/access-ip-rules/{rule_id}") in routes
        assert ("DELETE", "/admin/access-ip-rules/{rule_id}") in routes
        logger.info("test_access_policy_admin_routes_are_registered 完成")

    # 方法作用：验证数据库动态策略不能配置 public 或 optional 扩大匿名面。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_dynamic_public_policy_is_rejected_by_schema(self) -> None:
        """公开和可选认证只能由受版本控制的 YAML 基线声明。"""
        logger.debug("test_dynamic_public_policy_is_rejected_by_schema 入口")
        from src.api.routes.access_policy import AccessPolicyCreateRequest

        with pytest.raises(ValidationError):
            AccessPolicyCreateRequest(
                policy_key="unsafe-public",
                path="/api/v1/unsafe",
                methods=["GET"],
                auth_mode="public",
                access_log_mode="none",
            )
        logger.info("test_dynamic_public_policy_is_rejected_by_schema 完成")

    # 方法作用：验证创建动态策略后刷新内存快照并返回数据库记录。
    # Args: self - pytest 测试类实例；monkeypatch - 身份、数据库和策略管理器补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_dynamic_policy_refreshes_snapshot(self, monkeypatch) -> None:
        """事务写入成功后当前进程必须立即使用新策略。"""
        logger.debug("test_create_dynamic_policy_refreshes_snapshot 入口")
        import src.api.auth as auth
        import src.memory.pg_pool as pg_pool
        import src.security.api_access_policy as policy_module
        from src.api.routes.access_policy import AccessPolicyCreateRequest, create_access_policy

        row = {
            "id": 12,
            "policy_key": "reports",
            "path": "/api/v1/reports",
            "path_type": "exact",
            "methods": ["GET"],
            "auth_mode": "jwt",
            "access_log_mode": "standard",
            "priority": 0,
            "enabled": True,
            "description": "报表",
        }
        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value=row)
        connection_context = MagicMock()
        connection_context.__aenter__ = AsyncMock(return_value=connection)
        connection_context.__aexit__ = AsyncMock(return_value=False)
        manager = MagicMock()
        manager.refresh = AsyncMock()
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 1)
        monkeypatch.setattr(pg_pool, "pg_connection", MagicMock(return_value=connection_context))
        monkeypatch.setattr(policy_module, "get_api_access_policy_manager", lambda: manager)

        result = await create_access_policy(AccessPolicyCreateRequest(
            policy_key="reports",
            path="/api/v1/reports",
            methods=["GET"],
            auth_mode="jwt",
            access_log_mode="standard",
            description="报表",
        ))

        assert result["id"] == 12
        manager.refresh.assert_awaited_once()
        connection.fetchrow.assert_awaited_once()
        logger.info("test_create_dynamic_policy_refreshes_snapshot 完成")

    # 方法作用：验证 YAML 基线策略可添加 IP 规则但必须先存在于合并快照。
    # Args: self - pytest 测试类实例；monkeypatch - 身份和策略管理器补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_ip_rule_rejects_unknown_policy_key(self, monkeypatch) -> None:
        """拼写错误的策略键不能留下永远不生效的孤立规则。"""
        logger.debug("test_ip_rule_rejects_unknown_policy_key 入口")
        from fastapi import HTTPException

        import src.api.auth as auth
        import src.security.api_access_policy as policy_module
        from src.api.routes.access_policy import IpRuleCreateRequest, create_ip_rule

        manager = MagicMock()
        manager.has_policy.return_value = False
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(policy_module, "get_api_access_policy_manager", lambda: manager)

        with pytest.raises(HTTPException) as exc_info:
            await create_ip_rule(
                "missing",
                IpRuleCreateRequest(action="deny", cidr="203.0.113.0/24"),
            )

        assert exc_info.value.status_code == 404
        logger.info("test_ip_rule_rejects_unknown_policy_key 完成")
