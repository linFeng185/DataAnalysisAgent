"""已接受 auth 超时组件的补充日志回归：真实中间件决策 + Fake 身份库边界。

边界：只替换数据库边界（``_load_current_identity``）、设置取值与租户策略实例，
``AuthMiddleware._authenticate_request`` 的决策、503 边界、日志与取消路径全部真实；
不触网络/PG/外置配置，不重复 I 的真实 HTTP 用例，不改既有测试。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from structlog.testing import capture_logs

from src.api import auth as auth_module
from src.api.auth import AuthMiddleware
from src.security.api_access_policy import AccessLogMode, AccessPolicy, AuthMode, PathType
from src.security.tenant_policy import TenantPolicy

_IDENTITY = (4, 1, "tenant_admin", "default", "u4")
_SECRET_TEXT = "s3cr3t-timeout-DSN-postgresql://user:pw@host/db"


# 作用：下游 ASGI 替身：本文件从不调用它，只满足构造签名。
# Args: scope/receive/send - ASGI 参数。
# Returns: 无返回值。
async def _noop_app(scope: Any, receive: Any, send: Any) -> None:
    """占位下游应用，不参与决策。"""
    del scope, receive, send


# 作用：构造可离线决策的真实访问策略（JWT 模式）。
# Args: 无。
# Returns: AccessPolicy。
def _policy() -> AccessPolicy:
    """真实策略对象，避免回退读取全局配置。"""
    return AccessPolicy(
        policy_key="market_probe",
        path="/api/v1/market/probe",
        path_type=PathType.EXACT,
        methods=("GET",),
        auth_mode=AuthMode.JWT,
        access_log_mode=AccessLogMode.NONE,
        source="test",
    )


# 作用：构造带策略 state 与 Bearer 头的 GET 请求。
# Args: token - 已签名 JWT。
# Returns: starlette Request。
def _request(token: str) -> Request:
    """仅注入策略与 Authorization 头，不携带 Cookie。"""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/market/probe",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"",
        "state": {"api_access_policy": _policy()},
    }
    return Request(scope)


# 作用：身份库边界替身：按脚本返回身份或抛出异常。
# Args: 无。
# Returns: 无返回值。
class _Loader:
    """只替代数据库读取，不改中间件决策与日志。"""

    # 作用：绑定脚本结果。
    # Args: self - 替身；result - 身份元组或异常实例。
    # Returns: 无返回值。
    def __init__(self, result: Any) -> None:
        """保存脚本结果与调用次数。"""
        self.result = result
        self.calls = 0

    # 作用：回放脚本结果。
    # Args: self - 替身；user_id - JWT 声明的用户号。
    # Returns: 身份元组；脚本为异常时原样抛出。
    async def __call__(self, user_id: int) -> Any:
        """异常（含 CancelledError）不吞、不转换。"""
        del user_id
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


# 作用：装配真实中间件并只替换 DB 边界、设置与租户策略取值。
# Args: monkeypatch - 注入夹具；loader - 身份库边界替身。
# Returns: 已绑定替身边界的 AuthMiddleware。
def _middleware(monkeypatch: pytest.MonkeyPatch, loader: _Loader) -> AuthMiddleware:
    """固定测试密钥与假设置，不读真实 .env/config/app.yaml。"""
    settings = SimpleNamespace(
        database_url="postgresql://ignored",
        admin_api_key="",
        env="prod",
        jwt_access_token_expire_hours=1,
    )
    monkeypatch.setattr(auth_module, "_secret_cache", "unit-test-secret-0123456789-abcdefghij-kl")
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_module, "get_tenant_policy", lambda: TenantPolicy(multi_tenant=False))
    middleware = AuthMiddleware(app=_noop_app)
    monkeypatch.setattr(middleware, "_load_current_identity", loader)
    return middleware


# 作用：签发与 _IDENTITY 一致的真实 JWT。
# Args: 无。
# Returns: 已签名令牌字符串。
def _token() -> str:
    """复用产品签发函数，签名密钥来自固定测试缓存。"""
    return auth_module.create_access_token(*_IDENTITY)


class TestAuthTimeoutLoggingAcceptedComponent:
    """已接受 auth 超时组件：重复核验零 INFO、单次超时一条安全 ERROR、取消不记。"""

    # 作用：重复正常身份核验不得新增 INFO。
    # Args: self - 测试实例；monkeypatch - 注入夹具。
    # Returns: 无返回值。
    async def test_repeated_identity_reverification_adds_no_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """连续 64 次成功核验：无 denial、无 INFO 条目、调用计数一致。"""
        # Arrange
        loader = _Loader(_IDENTITY)
        middleware = _middleware(monkeypatch, loader)
        token = _token()
        # Act
        with capture_logs() as logs:
            for _ in range(64):
                identity, denial = await middleware._authenticate_request(_request(token))
                # Assert
                assert denial is None and identity == _IDENTITY
        assert loader.calls == 64
        assert [entry for entry in logs if entry.get("log_level") == "info"] == []

    # 作用：单次超时恰一条 ERROR（exc_info=True、error_type），原文与凭证不入载荷。
    # Args: self - 测试实例；monkeypatch - 注入夹具。
    # Returns: 无返回值。
    async def test_single_timeout_logs_one_safe_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ERROR 计数为 1，结构化载荷只含 error_type，不含 DSN/异常原文。"""
        # Arrange
        middleware = _middleware(monkeypatch, _Loader(TimeoutError(_SECRET_TEXT)))
        # Act
        with capture_logs() as logs:
            identity, denial = await middleware._authenticate_request(_request(_token()))
        # Assert
        assert identity is None and denial is not None and denial.status_code == 503
        errors = [entry for entry in logs if entry.get("log_level") == "error"]
        assert len(errors) == 1, f"单次超时应恰一条 ERROR：{logs}"
        entry = errors[0]
        assert entry.get("exc_info") is True
        assert entry.get("error_type") == "TimeoutError"
        assert entry.get("error") is None
        text = " ".join(str(dict(item)) for item in logs)
        assert _SECRET_TEXT not in text and "postgresql://" not in text

    # 作用：503 响应为通用 JSON 且不下发删 Cookie 头。
    # Args: self - 测试实例；monkeypatch - 注入夹具。
    # Returns: 无返回值。
    async def test_503_response_keeps_cookie(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 Set-Cookie 删除头、无凭证回显。"""
        # Arrange
        middleware = _middleware(monkeypatch, _Loader(RuntimeError(_SECRET_TEXT)))
        # Act
        identity, denial = await middleware._authenticate_request(_request(_token()))
        # Assert
        assert identity is None and denial is not None
        assert denial.status_code == 503
        header_names = {name.lower() for name in denial.headers}
        assert "set-cookie" not in header_names
        assert _SECRET_TEXT not in denial.body.decode("utf-8")

    # 作用：取消原样传播且不产生 ERROR 日志。
    # Args: self - 测试实例；monkeypatch - 注入夹具。
    # Returns: 无返回值。
    async def test_cancellation_propagates_without_error_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CancelledError 逃逸，本路径零 ERROR。"""
        # Arrange
        middleware = _middleware(monkeypatch, _Loader(asyncio.CancelledError()))
        # Act / Assert
        with capture_logs() as logs:
            with pytest.raises(asyncio.CancelledError):
                await middleware._authenticate_request(_request(_token()))
        assert [entry for entry in logs if entry.get("log_level") == "error"] == []
