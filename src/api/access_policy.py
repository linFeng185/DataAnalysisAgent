"""统一 API IP 阻断和分级访问日志纯 ASGI 中间件。"""

from __future__ import annotations

import time
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.logging_config import get_logger
from src.security.api_access_policy import (
    AccessLogMode,
    ApiAccessPolicyManager,
    get_api_access_policy_manager,
    resolve_client_ip,
)


logger = get_logger(__name__)


class ApiAccessPolicyMiddleware:
    """在认证前执行接口 IP 策略并统一输出访问摘要。"""

    # 方法作用：保存下游应用和可选的测试策略管理器。
    # Args: app - 下游 ASGI 应用；manager - 可选固定策略管理器。
    # Returns: 无返回值。
    def __init__(self, app: ASGIApp, *, manager: ApiAccessPolicyManager | None = None) -> None:
        logger.debug("ApiAccessPolicyMiddleware.__init__ 入口")
        self.app = app
        self.manager = manager
        logger.info("ApiAccessPolicyMiddleware.__init__ 完成", injected=manager is not None)

    # 方法作用：解析策略、阻断非法 IP，并在请求完成后输出一条分级访问摘要。
    # Args: scope - ASGI 作用域；receive - 接收通道；send - 发送通道。
    # Returns: 无返回值。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        manager = self.manager or get_api_access_policy_manager()
        client_ip = resolve_client_ip(scope, manager.trusted_proxy_cidrs)
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        decision = manager.resolve(path, method, client_ip)
        state = scope.setdefault("state", {})
        state["api_access_policy"] = decision.policy
        state["client_ip"] = decision.client_ip
        if not decision.allowed:
            logger.warning(
                "API IP 策略拒绝",
                path=path,
                method=method,
                client_ip=decision.client_ip,
                policy_key=decision.policy.policy_key,
                reason=decision.denial_reason,
            )
            response = JSONResponse({"detail": "当前 IP 不允许访问此接口"}, status_code=403)
            await response(scope, receive, send)
            return

        status_code = 500
        started = time.perf_counter()

        # 方法作用：捕获下游响应状态并原样转发 ASGI 消息。
        # Args: message - 下游响应消息。
        # Returns: 无返回值。
        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            logger.error(
                "API 访问异常",
                path=path,
                method=method,
                client_ip=decision.client_ip,
                policy_key=decision.policy.policy_key,
                error=str(exc),
                exc_info=True,
            )
            raise
        finally:
            if decision.policy.access_log_mode is not AccessLogMode.NONE:
                event = {
                    AccessLogMode.STANDARD: "API 访问完成",
                    AccessLogMode.SECURITY: "API 安全访问完成",
                    AccessLogMode.AUDIT: "API 审计访问完成",
                }[decision.policy.access_log_mode]
                logger.info(
                    event,
                    path=path,
                    method=method,
                    status_code=status_code,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    client_ip=decision.client_ip,
                    policy_key=decision.policy.policy_key,
                )
