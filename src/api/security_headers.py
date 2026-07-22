"""API 浏览器安全响应头纯 ASGI 中间件。"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.logging_config import get_logger


logger = get_logger(__name__)
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class _SecurityHeaderSender:
    """为 ASGI http.response.start 消息追加安全响应头。"""

    # 方法作用：保存原始发送通道和当前请求安全策略。
    # Args: send - 原始 ASGI 发送通道；scheme - 请求协议；production - 是否生产环境；hsts_seconds - HSTS 时长。
    # Returns: 无返回值。
    def __init__(
        self,
        send: Send,
        *,
        scheme: str,
        production: bool,
        hsts_seconds: int,
    ) -> None:
        logger.debug("安全响应头发送器初始化入口", scheme=scheme, production=production)
        self._send = send
        self._scheme = scheme
        self._production = production
        self._hsts_seconds = hsts_seconds
        logger.info("安全响应头发送器初始化完成", scheme=scheme, production=production)

    # 方法作用：在响应开始消息中写入安全头后转发给服务器。
    # Args: message - 当前 ASGI 响应消息。
    # Returns: 无返回值。
    async def __call__(self, message: Message) -> None:
        message_type = message.get("type", "")
        logger.debug("安全响应头发送入口", message_type=message_type)
        if message_type == "http.response.start":
            headers = MutableHeaders(scope=message)
            headers.setdefault("X-Content-Type-Options", "nosniff")
            headers.setdefault("X-Frame-Options", "DENY")
            headers.setdefault("Referrer-Policy", "no-referrer")
            headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if self._production:
                headers.setdefault("Content-Security-Policy", _API_CSP)
                if self._scheme == "https" and self._hsts_seconds > 0:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        f"max-age={self._hsts_seconds}; includeSubDomains",
                    )
            logger.info(
                "安全响应头写入完成",
                production=self._production,
                hsts=self._production and self._scheme == "https" and self._hsts_seconds > 0,
            )
        await self._send(message)
        if message_type == "http.response.start":
            logger.info("安全响应头发送完成", message_type=message_type)
        else:
            logger.debug("安全响应体分块发送完成", message_type=message_type)


class SecurityHeadersMiddleware:
    """为 HTTP 响应添加 CSP、HSTS 和内容嗅探防护。"""

    # 方法作用：保存下游应用和环境级安全头配置。
    # Args: app - 下游 ASGI 应用；production - 是否生产环境；hsts_seconds - HSTS max-age。
    # Returns: 无返回值。
    def __init__(
        self,
        app: ASGIApp,
        *,
        production: bool,
        hsts_seconds: int = 31_536_000,
    ) -> None:
        logger.debug("安全响应头中间件初始化入口", production=production)
        self.app = app
        self.production = production
        self.hsts_seconds = max(0, int(hsts_seconds))
        logger.info(
            "安全响应头中间件初始化完成",
            production=production,
            hsts_seconds=self.hsts_seconds,
        )

    # 方法作用：为 HTTP 请求包装发送通道，其他 ASGI 协议直接透传。
    # Args: scope - ASGI 作用域；receive - 接收通道；send - 发送通道。
    # Returns: 无返回值。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type", "")
        logger.debug("安全响应头中间件入口", scope_type=scope_type, path=scope.get("path", ""))
        if scope_type != "http":
            await self.app(scope, receive, send)
            logger.info("安全响应头中间件完成", scope_type=scope_type, mode="passthrough")
            return
        wrapped_send = _SecurityHeaderSender(
            send,
            scheme=str(scope.get("scheme", "http")),
            production=self.production,
            hsts_seconds=self.hsts_seconds,
        )
        await self.app(scope, receive, wrapped_send)
        logger.info("安全响应头中间件完成", scope_type=scope_type, path=scope.get("path", ""))
