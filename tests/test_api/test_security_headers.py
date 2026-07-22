"""API 安全响应头和 CORS 中间件回归测试。"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.cors import CORSMiddleware


logger = logging.getLogger(__name__)


class TestSecurityMiddlewareStack:
    """覆盖生产安全响应头和 CORS 注册。"""

    # 方法作用：验证 HTTPS 响应带有浏览器安全头。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_https_response_has_security_headers(self) -> None:
        """生产 HTTPS 响应必须启用 CSP、HSTS、防嵌入和 nosniff。"""
        logger.debug("test_https_response_has_security_headers 入口")
        try:
            # Arrange
            from src.api.security_headers import SecurityHeadersMiddleware

            app = FastAPI()

            # 方法作用：为安全头测试返回最小成功响应。
            # Args: 无。
            # Returns: 固定健康状态字典。
            @app.get("/health")
            async def health() -> dict[str, str]:
                return {"status": "ok"}

            app.add_middleware(
                SecurityHeadersMiddleware,
                production=True,
                hsts_seconds=31_536_000,
            )
            transport = ASGITransport(app=app)

            # Act
            async with AsyncClient(transport=transport, base_url="https://test") as client:
                response = await client.get("/health")

            # Assert
            assert response.headers["content-security-policy"]
            assert response.headers["strict-transport-security"] == (
                "max-age=31536000; includeSubDomains"
            )
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["x-content-type-options"] == "nosniff"
            logger.info("test_https_response_has_security_headers 完成")
        except Exception as exc:
            logger.error(
                "test_https_response_has_security_headers 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证应用工厂注册 CORS 与安全响应头中间件。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_application_registers_cors_and_security_headers(self, monkeypatch) -> None:
        """API 中间件栈必须显式包含 CORS 和安全响应头。"""
        logger.debug("test_application_registers_cors_and_security_headers 入口")
        try:
            # Arrange
            from types import SimpleNamespace

            import src.config as config_module
            import src.main as main_module
            from src.api.security_headers import SecurityHeadersMiddleware

            settings = SimpleNamespace(
                env="test",
                cors_allowed_origins="https://app.example.com",
                security_hsts_seconds=31_536_000,
            )
            monkeypatch.setattr(main_module, "get_settings", lambda: settings)
            monkeypatch.setattr(config_module, "validate_production_settings", lambda value: None)

            # Act
            app = main_module.create_app()
            middleware_classes = [item.cls for item in app.user_middleware]

            # Assert
            assert CORSMiddleware in middleware_classes
            assert SecurityHeadersMiddleware in middleware_classes
            logger.info("test_application_registers_cors_and_security_headers 完成")
        except Exception as exc:
            logger.error(
                "test_application_registers_cors_and_security_headers 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证 CORS 只允许配置中的精确前端 origin。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_cors_preflight_echoes_only_allowed_origin(self) -> None:
        """未列入 allowlist 的站点不得获得跨域授权响应头。"""
        logger.debug("test_cors_preflight_echoes_only_allowed_origin 入口")
        try:
            # Arrange
            app = FastAPI()
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["https://app.example.com"],
                allow_credentials=True,
                allow_methods=["GET", "POST"],
                allow_headers=["Authorization", "Content-Type"],
            )
            transport = ASGITransport(app=app)
            headers = {
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization",
            }

            # Act
            async with AsyncClient(transport=transport, base_url="https://api.example.com") as client:
                allowed = await client.options(
                    "/api/v1/chat",
                    headers={**headers, "Origin": "https://app.example.com"},
                )
                blocked = await client.options(
                    "/api/v1/chat",
                    headers={**headers, "Origin": "https://evil.example.com"},
                )

            # Assert
            assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
            assert "access-control-allow-origin" not in blocked.headers
            logger.info("test_cors_preflight_echoes_only_allowed_origin 完成")
        except Exception as exc:
            logger.error(
                "test_cors_preflight_echoes_only_allowed_origin 异常: %s",
                exc,
                exc_info=True,
            )
            raise
