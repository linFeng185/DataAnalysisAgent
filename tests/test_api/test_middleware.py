"""全局异常处理中间件直接单元测试。"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


logger = logging.getLogger(__name__)


class TestExceptionMiddleware:
    """覆盖功能 1.3.7：异常到 HTTP 响应的安全映射。"""

    # 方法作用：验证未知异常返回脱敏 500 且日志包含堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_generic_exception_is_sanitized_and_logged(self, monkeypatch) -> None:
        """内部异常文本不得返回客户端，但必须保留服务端完整诊断信息。"""
        logger.debug("test_generic_exception_is_sanitized_and_logged 入口")
        try:
            # Arrange
            import src.api.middleware as middleware_module

            app = FastAPI()

            # 方法作用：模拟携带敏感内部细节的未处理异常。
            # Args: 无。
            # Returns: 不返回响应，固定抛出 RuntimeError。
            @app.get("/failure")
            async def failure() -> dict:
                raise RuntimeError("database password leaked")

            middleware_module.register_exception_handlers(app)
            captured_logger = MagicMock()
            monkeypatch.setattr(middleware_module, "logger", captured_logger)
            transport = ASGITransport(app=app, raise_app_exceptions=False)

            # Act
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/failure")

            # Assert
            assert response.status_code == 500
            assert response.json()["error_message"] == "服务器内部错误"
            assert "password" not in response.text
            captured_logger.error.assert_called_once()
            assert captured_logger.error.call_args.kwargs["exc_info"] is True
            logger.info("test_generic_exception_is_sanitized_and_logged 完成")
        except Exception as exc:
            logger.error(
                "test_generic_exception_is_sanitized_and_logged 异常: %s",
                exc,
                exc_info=True,
            )
            raise
