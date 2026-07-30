"""API 访问策略配置、匹配、IP 规则和日志中间件测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

logger = logging.getLogger(__name__)


class TestApiAccessPolicyConfig:
    """覆盖功能 22.1.1：YAML 启动策略配置。"""

    # 方法作用：验证结构化访问配置能规范化方法并保留认证与日志模式。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_structured_policy_config_normalizes_methods(self) -> None:
        """方法名必须转为大写，公开和静默模式应保持结构化字段。"""
        logger.debug("test_structured_policy_config_normalizes_methods 入口")
        from src.config import Settings

        settings = Settings(
            _env_file=None,
            env="test",
            api_access={
                "default_auth": "jwt",
                "default_access_log": "standard",
                "bootstrap_policies": [
                    {
                        "id": "probe",
                        "path": "/probe",
                        "path_type": "exact",
                        "methods": ["get"],
                        "auth": "public",
                        "access_log": "none",
                    },
                ],
            },
        )

        policy = settings.api_access.bootstrap_policies[0]

        assert policy.methods == ["GET"]
        assert policy.auth == "public"
        assert policy.access_log == "none"
        logger.info("test_structured_policy_config_normalizes_methods 完成")

    # 方法作用：验证重复策略编号在应用启动前被配置校验拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_duplicate_bootstrap_policy_ids_are_rejected(self) -> None:
        """策略编号是数据库 IP 规则引用键，不能重复。"""
        logger.debug("test_duplicate_bootstrap_policy_ids_are_rejected 入口")
        from src.config import Settings

        item = {
            "id": "duplicate",
            "path": "/probe",
            "methods": ["GET"],
            "auth": "public",
            "access_log": "standard",
        }

        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                env="test",
                api_access={"bootstrap_policies": [item, {**item, "path": "/other"}]},
            )
        logger.info("test_duplicate_bootstrap_policy_ids_are_rejected 完成")


class TestApiAccessPolicyResolution:
    """覆盖功能 22.1.2、22.2.1 和 22.2.2。"""

    # 方法作用：验证精确路径必须同时匹配 HTTP 方法，未知请求默认要求 JWT。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_exact_policy_is_method_scoped_and_default_closed(self) -> None:
        """GET 健康检查公开不能使同路径 POST 也变成公开接口。"""
        logger.debug("test_exact_policy_is_method_scoped_and_default_closed 入口")
        from src.config import ApiAccessConfig
        from src.security.api_access_policy import ApiAccessPolicyManager, AuthMode

        manager = ApiAccessPolicyManager(SimpleNamespace(api_access=ApiAccessConfig()))

        get_decision = manager.resolve("/api/v1/health", "GET", "203.0.113.7")
        post_decision = manager.resolve("/api/v1/health", "POST", "203.0.113.7")

        assert get_decision.policy.auth_mode is AuthMode.PUBLIC
        assert post_decision.policy.auth_mode is AuthMode.JWT
        assert get_decision.allowed is True
        logger.info("test_exact_policy_is_method_scoped_and_default_closed 完成")

    # 方法作用：验证模板策略匹配路径参数且保留管理 Key 认证模式。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_template_policy_matches_path_parameters(self) -> None:
        """数据源删除接口应由 YAML 模板替代代码中的 startswith 判断。"""
        logger.debug("test_template_policy_matches_path_parameters 入口")
        from src.config import ApiAccessConfig
        from src.security.api_access_policy import ApiAccessPolicyManager, AuthMode

        manager = ApiAccessPolicyManager(SimpleNamespace(api_access=ApiAccessConfig()))

        decision = manager.resolve("/api/v1/datasources/mysql-prod", "DELETE", "203.0.113.8")

        assert decision.policy.policy_key == "datasource_delete"
        assert decision.policy.auth_mode is AuthMode.JWT_OR_ADMIN_KEY
        logger.info("test_template_policy_matches_path_parameters 完成")

    # 方法作用：验证只有可信代理来源才能使用 X-Forwarded-For 计算客户端地址。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_forwarded_ip_is_used_only_for_trusted_proxy(self) -> None:
        """不可信直连方不能伪造转发头绕过接口 IP 规则。"""
        logger.debug("test_forwarded_ip_is_used_only_for_trusted_proxy 入口")
        from src.security.api_access_policy import resolve_client_ip

        trusted_scope = {
            "client": ("10.0.0.4", 5000),
            "headers": [(b"x-forwarded-for", b"198.51.100.7, 10.0.0.3")],
        }
        untrusted_scope = {
            "client": ("203.0.113.9", 5000),
            "headers": [(b"x-forwarded-for", b"198.51.100.7")],
        }

        trusted = resolve_client_ip(trusted_scope, ("10.0.0.0/8",))
        untrusted = resolve_client_ip(untrusted_scope, ("10.0.0.0/8",))

        assert str(trusted) == "198.51.100.7"
        assert str(untrusted) == "203.0.113.9"
        logger.info("test_forwarded_ip_is_used_only_for_trusted_proxy 完成")

    # 方法作用：验证接口 deny 优先且配置 allow 后对未命中地址默认拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_ip_deny_precedes_allow_and_allowlist_fails_closed(self) -> None:
        """白名单网段中的单 IP 黑名单仍应拒绝，网段外地址也应拒绝。"""
        logger.debug("test_ip_deny_precedes_allow_and_allowlist_fails_closed 入口")
        from src.config import ApiAccessConfig
        from src.security.api_access_policy import ApiAccessPolicyManager

        manager = ApiAccessPolicyManager(SimpleNamespace(api_access=ApiAccessConfig()))
        manager.replace_dynamic(
            [
                {
                    "id": 9,
                    "policy_key": "reports",
                    "path": "/api/v1/reports",
                    "path_type": "exact",
                    "methods": ["GET"],
                    "auth_mode": "jwt",
                    "access_log_mode": "standard",
                    "priority": 10,
                    "enabled": True,
                    "description": "报表接口",
                },
            ],
            [
                {"id": 1, "policy_key": "reports", "action": "allow", "cidr": "198.51.100.0/24", "enabled": True},
                {"id": 2, "policy_key": "reports", "action": "deny", "cidr": "198.51.100.7/32", "enabled": True},
            ],
        )

        allowed = manager.resolve("/api/v1/reports", "GET", "198.51.100.8")
        denied_override = manager.resolve("/api/v1/reports", "GET", "198.51.100.7")
        denied_outside = manager.resolve("/api/v1/reports", "GET", "203.0.113.9")

        assert allowed.allowed is True
        assert denied_override.allowed is False
        assert denied_outside.allowed is False
        logger.info("test_ip_deny_precedes_allow_and_allowlist_fails_closed 完成")

    # 方法作用：验证全局紧急 CIDR 黑名单在未知路径默认策略前完成阻断。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_emergency_ip_deny_blocks_unknown_route(self) -> None:
        """紧急阻断不依赖接口是否已有数据库策略。"""
        logger.debug("test_emergency_ip_deny_blocks_unknown_route 入口")
        from src.config import ApiAccessConfig
        from src.security.api_access_policy import ApiAccessPolicyManager

        config = ApiAccessConfig(emergency_ip_deny=["2001:db8::/32"])
        manager = ApiAccessPolicyManager(SimpleNamespace(api_access=config))

        decision = manager.resolve("/api/v1/unknown", "GET", "2001:db8::10")

        assert decision.allowed is False
        assert decision.denial_reason == "emergency_deny"
        logger.info("test_emergency_ip_deny_blocks_unknown_route 完成")


class TestApiAccessPolicyMiddleware:
    """覆盖功能 22.2.3：分级访问日志纯 ASGI 中间件。"""

    # 方法作用：验证静默策略不输出成功访问摘要，普通策略仍输出聚合日志。
    # Args: self - pytest 测试类实例；monkeypatch - 日志补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_none_suppresses_access_summary_but_standard_logs(self, monkeypatch) -> None:
        """健康检查可静默，但普通接口必须保留一条访问摘要。"""
        logger.debug("test_none_suppresses_access_summary_but_standard_logs 入口")
        import src.api.access_policy as middleware_module
        from src.api.access_policy import ApiAccessPolicyMiddleware
        from src.config import ApiAccessConfig
        from src.security.api_access_policy import ApiAccessPolicyManager

        app = FastAPI()

        # 方法作用：提供静默健康检查响应。
        # Args: 无。
        # Returns: 健康状态。
        @app.get("/api/v1/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        # 方法作用：提供普通受保护路径的下游响应。
        # Args: 无。
        # Returns: 固定响应。
        @app.get("/api/v1/private")
        async def private() -> dict[str, bool]:
            return {"ok": True}

        captured_logger = MagicMock()
        monkeypatch.setattr(middleware_module, "logger", captured_logger)
        manager = ApiAccessPolicyManager(SimpleNamespace(api_access=ApiAccessConfig()))
        middleware = ApiAccessPolicyMiddleware(app, manager=manager)
        captured_logger.reset_mock()
        transport = ASGITransport(app=middleware)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health_response = await client.get("/api/v1/health")
            private_response = await client.get("/api/v1/private")

        assert health_response.status_code == 200
        assert private_response.status_code == 200
        access_events = [call.args[0] for call in captured_logger.info.call_args_list]
        assert "API 访问完成" in access_events
        assert access_events.count("API 访问完成") == 1
        logger.info("test_none_suppresses_access_summary_but_standard_logs 完成")

    # 方法作用：验证 IP 拒绝即使接口配置静默也必须记录安全日志。
    # Args: self - pytest 测试类实例；monkeypatch - 日志补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_ip_denial_is_always_logged(self, monkeypatch) -> None:
        """静默仅影响成功访问摘要，不能隐藏安全阻断。"""
        logger.debug("test_ip_denial_is_always_logged 入口")
        import src.api.access_policy as middleware_module
        from src.api.access_policy import ApiAccessPolicyMiddleware
        from src.config import ApiAccessConfig
        from src.security.api_access_policy import ApiAccessPolicyManager

        captured_logger = MagicMock()
        monkeypatch.setattr(middleware_module, "logger", captured_logger)
        manager = ApiAccessPolicyManager(SimpleNamespace(
            api_access=ApiAccessConfig(emergency_ip_deny=["127.0.0.1/32"]),
        ))
        app = MagicMock()
        middleware = ApiAccessPolicyMiddleware(app, manager=manager)
        captured_logger.reset_mock()
        transport = ASGITransport(app=middleware)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")

        assert response.status_code == 403
        captured_logger.warning.assert_called_once()
        assert captured_logger.warning.call_args.args[0] == "API IP 策略拒绝"
        app.assert_not_called()
        logger.info("test_ip_denial_is_always_logged 完成")


class TestApiAccessPolicyAuthIntegration:
    """覆盖功能 22.1.2：访问策略到认证中间件的模式传递。"""

    # 方法作用：验证 super_admin 策略拒绝普通有效 JWT。
    # Args: self - pytest 测试类实例；monkeypatch - 配置与租户策略补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_super_admin_policy_rejects_regular_jwt(self, monkeypatch) -> None:
        """有效身份不等于平台管理员身份，固定管理策略必须二次校验。"""
        logger.debug("test_super_admin_policy_rejects_regular_jwt 入口")
        from starlette.requests import Request
        from starlette.responses import Response

        import src.api.auth as auth
        from src.config import ApiAccessConfig
        from src.security.api_access_policy import ApiAccessPolicyManager
        from src.security.tenant_policy import TenantPolicy

        settings = SimpleNamespace(
            api_access=ApiAccessConfig(),
            admin_api_key="",
            jwt_secret="z" * 32,
            jwt_access_token_expire_hours=24,
            env="test",
        )
        monkeypatch.setattr(auth, "get_settings", lambda: settings)
        monkeypatch.setattr(auth, "get_tenant_policy", lambda: TenantPolicy(multi_tenant=False))
        monkeypatch.setattr(auth, "_secret_cache", None)
        token = auth.create_access_token(2, 1, "analyst")
        policy = ApiAccessPolicyManager(settings).resolve(
            "/api/v1/admin/access-policies", "GET", "127.0.0.1",
        ).policy
        path = "/api/v1/admin/access-policies"
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
            "query_string": b"", "client": ("127.0.0.1", 5000),
            "server": ("test", 80),
            "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            "state": {"api_access_policy": policy},
        }
        middleware = auth.AuthMiddleware(AsyncMock())

        response = await middleware.dispatch(
            Request(scope), AsyncMock(return_value=Response("ok")),
        )

        assert response.status_code == 401
        logger.info("test_super_admin_policy_rejects_regular_jwt 完成")

    # 方法作用：验证应用工厂把访问策略中间件放在认证中间件外层。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_application_policy_middleware_precedes_auth(self) -> None:
        """认证中间件读取策略前，访问策略必须已经写入 ASGI scope。"""
        logger.debug("test_application_policy_middleware_precedes_auth 入口")
        from src.api.access_policy import ApiAccessPolicyMiddleware
        from src.api.auth import AuthMiddleware
        from src.main import create_app

        middleware_classes = [item.cls for item in create_app().user_middleware]

        assert ApiAccessPolicyMiddleware in middleware_classes
        assert middleware_classes.index(ApiAccessPolicyMiddleware) < middleware_classes.index(AuthMiddleware)
        logger.info("test_application_policy_middleware_precedes_auth 完成")
