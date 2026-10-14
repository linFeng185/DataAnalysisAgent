"""认证依赖故障 HTTP 边界：真实 JWT + 真实 AuthMiddleware + 仅身份查询边界 Fake。

合同依据（spec/61 §3：认证依赖故障 503，不当作登录失效）：
  数据库身份复核「暂时不可用」时必须 503，不得降级 401、不得沿用旧身份放行、
  不得清除会话 Cookie；明确无身份或数据库身份与 JWT 不一致必须 401；
  依赖恢复后同一 JWT 必须 200 且身份准确。

真实性边界：
  * 真：JWT 编解码、AuthMiddleware 决策、503/401 拒绝、Cookie 语义、ContextVar
    注入与恢复、取消传播、真实 HTTP 栈（127.0.0.1 随机端口 uvicorn + curl.exe）。
  * 假：仅数据库身份读取边界 auth.load_current_identity 按 user_id 脚本回放；
    AuthMiddleware 核心决策保持产品实现。
  * settings 为内存 SimpleNamespace，不读用户配置；JWT 密钥仅存进程内存，
    不落盘不输出；database_url 指向不可达回环端口，意外建连立即失败。
  * 不导入任何市场/筛选路由，不外呼，不触 PG 与用户现有服务。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.types import ASGIApp
from structlog.testing import capture_logs

import src.api.auth as auth
from src.app_context import AppContextMiddleware, create_app_context
from src.security.tenant_policy import (
    ANONYMOUS_ROLE,
    ANONYMOUS_USER_ID,
    DEFAULT_TENANT_ID,
    RequestIdentity,
)

PROTECTED_PATH = "/api/v1/unit/auth-probe"
TIMEOUT_DETAIL_MARKER = "SECRET-TIMEOUT-DSN-unit-auth-probe-36.2"
ANONYMOUS_CONTEXT: tuple[int, int, str, str, str] = (
    ANONYMOUS_USER_ID,
    DEFAULT_TENANT_ID,
    ANONYMOUS_ROLE,
    "default",
    "",
)
_SENTINEL_IDENTITY = RequestIdentity(
    tenant_id=77, user_id=33, role="viewer", username="outer-probe",
)
_SENTINEL_CONTEXT: tuple[int, int, str, str, str] = (33, 77, "viewer", "default", "outer-probe")


# 方法作用：构造仅存内存的认证边界测试 settings，绝不读取用户配置文件。
# Args: env - 部署环境（prod/dev）；jwt_secret - 本次测试的临时 JWT 密钥。
# Returns: 覆盖认证分支所需字段的 SimpleNamespace。
def _probe_settings(env: str, jwt_secret: str) -> SimpleNamespace:
    """database_url 指向不可达回环端口：只为开启身份复核分支，意外建连即时失败。"""
    return SimpleNamespace(
        env=env,
        multi_tenant=False,
        admin_api_key="",
        jwt_secret=jwt_secret,
        jwt_access_token_expire_hours=24,
        database_url="postgresql+asyncpg://unit_probe:never@127.0.0.1:1/unit_probe",
        registration_enabled=False,
    )


# 方法作用：构造数据库复核身份五元组与探针响应预期字典，两侧共用同一来源。
# Args: user_id - 用户 ID；tenant_id/role/tenant_code/username - 其余身份字段。
# Returns: (AuthIdentity 五元组, FastAPI 探针响应预期字典) 二元组。
def _identity_pair(
    user_id: int,
    tenant_id: int = 4,
    role: str = "analyst",
    tenant_code: str = "acme",
    username: str | None = None,
) -> tuple[auth.AuthIdentity, dict[str, Any]]:
    """username 缺省与 JWT 声明保持确定性一致，避免身份比较被夹具差异污染。"""
    name = username if username is not None else f"user{user_id}"
    identity: auth.AuthIdentity = (user_id, tenant_id, role, tenant_code, name)
    return identity, {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "tenant_code": tenant_code,
        "username": name,
    }


# 方法作用：用真实 create_access_token 为身份五元组签发 HS256 JWT。
# Args: identity - (user_id, tenant_id, role, tenant_code, username) 五元组。
# Returns: 真实 JWT 字符串；仅存内存，不落盘、不打印、不进断言消息。
def _mint_token(identity: auth.AuthIdentity) -> str:
    """声明与身份元组逐字段一致，保证复核比较只受被测分支影响。"""
    return auth.create_access_token(*identity)


# 方法作用：数据库身份读取边界替身：按 user_id 脚本回放，绝不触 PostgreSQL。
# Args: script - {user_id: 身份五元组 | 待抛异常 | None} 脚本表。
# Returns: 可安装为 auth.load_current_identity 的异步可调用对象。
class _ScriptedIdentityReader:
    """只替换 src/api/auth.load_current_identity；AuthMiddleware 核心保持产品实现。"""

    # 方法作用：保存脚本表与调用账本。
    # Args: self - 替身；script - 按 user_id 的结果表（身份/异常/None）。
    # Returns: 无返回值。
    def __init__(self, script: dict[int, Any]) -> None:
        """结果类型：AuthIdentity 五元组、要抛出的异常实例或 None（停用/不存在）。"""
        self.script = script
        self.calls: list[int] = []

    # 方法作用：按 user_id 回放脚本结果并记录调用。
    # Args: self - 替身；user_id - JWT 声明的用户 ID。
    # Returns: 身份五元组或 None；脚本为异常实例时原样抛出。
    async def __call__(self, user_id: int) -> auth.AuthIdentity | None:
        """未脚本化的 user_id 直接失败，避免静默 401 掩盖测试接线错误。"""
        self.calls.append(user_id)
        await asyncio.sleep(0)
        if user_id not in self.script:
            raise AssertionError(f"测试未脚本化 user_id={user_id}")
        outcome = self.script[user_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# 方法作用：安装闭环认证夹具：内存 settings、临时密钥、Fake 身份读取边界。
# Args: monkeypatch - pytest 补丁工具；env - 部署环境；reader - 身份读取替身。
# Returns: 已生效的内存 SimpleNamespace settings。
def _install_probe_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: str,
    reader: _ScriptedIdentityReader,
) -> SimpleNamespace:
    """密钥仅存进程内存（删环境变量 + 清缓存），不读取用户配置、不输出。"""
    secret = secrets.token_hex(32)
    settings = _probe_settings(env, secret)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setattr(auth, "_secret_cache", None)
    monkeypatch.setattr(auth, "load_current_identity", reader)
    return settings


# 方法作用：一次性读取五个身份 ContextVar，用于跨请求串线检查。
# Args: 无。
# Returns: (user_id, tenant_id, role, tenant_code, username) 五元组。
def _read_context_vars() -> tuple[int, int, str, str, str]:
    """只读真实 ContextVar，不写入或重置。"""
    return (
        auth.get_current_user_id(),
        auth.get_current_tenant_id(),
        auth.get_current_role(),
        auth.get_current_tenant_code(),
        auth.get_current_username(),
    )


# 方法作用：构造真实 ASGI 栈：FastAPI 探针路由 + 真实 AuthMiddleware + Context 绑定。
# Args: settings - 内存测试 settings。
# Returns: 可交由 ASGITransport 或 uvicorn 承载的 ASGI 应用。
def _probe_stack(settings: SimpleNamespace) -> ASGIApp:
    """探针路由只回读真实 ContextVar，不实现也未宣称任何业务接口。"""
    app = FastAPI()

    # 方法作用：回读五个真实身份 ContextVar，供 HTTP 侧断言身份与串线。
    # Args: 无。
    # Returns: 身份字段字典。
    @app.get(PROTECTED_PATH)
    async def auth_probe() -> dict[str, Any]:
        """认证边界路径上的只读探针，不触业务存储。"""
        return {
            "user_id": auth.get_current_user_id(),
            "tenant_id": auth.get_current_tenant_id(),
            "role": auth.get_current_role(),
            "tenant_code": auth.get_current_tenant_code(),
            "username": auth.get_current_username(),
        }

    return AppContextMiddleware(
        auth.AuthMiddleware(app),
        context=create_app_context(settings),
    )


# 方法作用：经 ASGITransport 对真实中间件栈发起一次 Bearer 认证的边界请求。
# Args: stack - ASGI 应用；token - 真实 JWT（只进请求头，不进日志与断言消息）。
# Returns: httpx 响应对象。
async def _call_probe_entry(stack: ASGIApp, token: str) -> Response:
    """每个用例独立客户端，避免连接层状态混入身份断言。"""
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://unit-probe") as client:
        return await client.get(PROTECTED_PATH, headers={"Authorization": f"Bearer {token}"})


# 方法作用：判断响应是否命令客户端清空会话 Cookie（等价于退出登录）。
# Args: response - httpx 响应。
# Returns: 存在清除 access_token 的 Set-Cookie 语义时返回 True。
def _clears_access_cookie(response: Response) -> bool:
    """只识别 Max-Age=0 或 Expires=1970 的清除语义，不读取或输出令牌值。"""
    for header in response.headers.get_list("set-cookie"):
        lowered = header.lower()
        if auth.ACCESS_TOKEN_COOKIE in lowered and (
            "max-age=0" in lowered or "expires=thu, 01 jan 1970" in lowered
        ):
            return True
    return False


# 方法作用：仅在 127.0.0.1 随机端口承载临时测试应用的 uvicorn 后台线程包装。
# Args: app - 待承载的临时 ASGI 应用。
# Returns: 可启动/停止并暴露实际端口的管理器实例。
class _LoopbackUvicornServer:
    """只监听回环地址；不请求用户现有服务，生命周期随用例结束。"""

    # 方法作用：绑定应用并配置静默日志。
    # Args: self - 管理器；app - 临时 ASGI 应用。
    # Returns: 无返回值。
    def __init__(self, app: ASGIApp) -> None:
        """port=0 由内核分配随机空闲端口，避免与用户服务冲突。"""
        self._server = uvicorn.Server(
            uvicorn.Config(
                app=app,
                host="127.0.0.1",
                port=0,
                log_level="error",
                access_log=False,
                lifespan="off",
            )
        )
        self._thread = threading.Thread(
            target=self._server.run, name="unit-auth-probe-uvicorn", daemon=True,
        )

    # 方法作用：读取内核分配的实际回环端口。
    # Args: self - 管理器。
    # Returns: 已就绪时返回端口号，未就绪返回 0。
    @property
    def port(self) -> int:
        """未 started 或尚无监听套接字时返回 0，供就绪轮询判断。"""
        if not self._server.started or not self._server.servers:
            return 0
        sockets = self._server.servers[0].sockets
        return int(sockets[0].getsockname()[1]) if sockets else 0

    # 方法作用：启动服务并等待就绪。
    # Args: self - 管理器；timeout_seconds - 就绪等待上限。
    # Returns: 无返回值；超时以 AssertionError 暴露而非跳过。
    async def start(self, timeout_seconds: float = 20.0) -> None:
        """就绪前不允许测试发请求，避免端口竞态被误读为产品行为。"""
        self._thread.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.port:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("uvicorn 未在期限内就绪")

    # 方法作用：请求退出并等待后台线程结束。
    # Args: self - 管理器。
    # Returns: 无返回值。
    def stop(self) -> None:
        """幂等收尾，保证临时服务不遗留监听端口。"""
        self._server.should_exit = True
        self._thread.join(timeout=10)


# 方法作用：对回环临时服务发起一次原始 HTTP GET，只返回状态码与传输工具标签。
# Args: url - 回环边界 URL；token - 真实 JWT；body_path - 可选响应体落盘路径；
#       timeout_seconds - 单次上限。
# Returns: (HTTP 状态码, "curl.exe") 二元组；未提供 body_path 时丢弃正文与响应头。
def _raw_status_loopback(
    url: str,
    token: str,
    body_path: str | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[int, str]:
    """仅使用 curl.exe；JWT 经 stdin 配置传入，不进 argv、不进终端输出。"""
    curl_path = shutil.which("curl.exe") or shutil.which("curl")
    if curl_path is None:
        raise AssertionError("缺少 curl.exe，无法完成真实回环验收")
    output = body_path if body_path is not None else os.devnull
    config = f'url = "{url}"\nheader = "Authorization: Bearer {token}"\n'
    command = [
        curl_path, "-s", "-S", "--noproxy", "*",
        "-o", output, "-w", "%{http_code}", "--max-time", str(int(timeout_seconds)),
        "-K", "-",
    ]
    completed = subprocess.run(
        command,
        input=config,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    status_text = completed.stdout.strip()
    if completed.returncode != 0 or not status_text.isdigit():
        raise AssertionError(f"curl.exe 请求失败: returncode={completed.returncode}")
    return int(status_text), "curl.exe"


class TestAuthDependencyHttpContract:
    """36.2：身份库故障、失效、恢复与上下文生命周期的真实 HTTP 边界。"""

    # 作用：验证 prod/dev 身份复核超时必须 503，而非 401/200。
    # Args: self - 测试实例；monkeypatch - 补丁夹具；env - 部署环境。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    @pytest.mark.parametrize("env", ["prod", "dev"])
    async def test_timeout_returns_503_not_401_or_200(
        self, monkeypatch: pytest.MonkeyPatch, env: str,
    ) -> None:
        """依赖暂不可用不是登录失效：503 通用文案、异常原文不泄漏。"""
        # Arrange
        identity, _ = _identity_pair(9)
        reader = _ScriptedIdentityReader({9: TimeoutError(TIMEOUT_DETAIL_MARKER)})
        settings = _install_probe_auth(monkeypatch, env=env, reader=reader)
        stack = _probe_stack(settings)
        token = _mint_token(identity)
        # Act
        response = await _call_probe_entry(stack, token)
        # Assert
        assert response.status_code == 503, (
            f"{env} 环境身份复核超时应为 503，实际 {response.status_code}"
        )
        assert response.json() == {"detail": "认证服务暂不可用"}
        assert TIMEOUT_DETAIL_MARKER not in response.text
        assert reader.calls == [9]
        assert _read_context_vars() == ANONYMOUS_CONTEXT

    # 作用：验证 503 不下发清空会话 Cookie 的指令，浏览器登录态保留。
    # Args: self - 测试实例；monkeypatch - 补丁夹具；env - 部署环境。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    @pytest.mark.parametrize("env", ["prod", "dev"])
    async def test_timeout_never_clears_session_cookie(
        self, monkeypatch: pytest.MonkeyPatch, env: str,
    ) -> None:
        """超时是服务暂不可用而非退出登录：不得清 Cookie，也不得泄露异常原文。"""
        # Arrange
        identity, _ = _identity_pair(9)
        reader = _ScriptedIdentityReader({9: TimeoutError(TIMEOUT_DETAIL_MARKER)})
        settings = _install_probe_auth(monkeypatch, env=env, reader=reader)
        stack = _probe_stack(settings)
        token = _mint_token(identity)
        # Act
        response = await _call_probe_entry(stack, token)
        # Assert
        assert response.status_code == 503
        assert not _clears_access_cookie(response)
        assert TIMEOUT_DETAIL_MARKER not in response.text

    # 作用：验证明确无身份或数据库身份与 JWT 不一致时必须 401。
    # Args: self - 测试实例；monkeypatch - 补丁夹具；case - 场景名；scripted - 脚本结果。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("case", "scripted"),
        [
            ("explicit_none", None),
            ("role_changed", (9, 4, "viewer", "acme", "user9")),
            ("tenant_changed", (9, 5, "analyst", "acme", "user9")),
        ],
    )
    async def test_invalid_or_changed_identity_still_401(
        self, monkeypatch: pytest.MonkeyPatch, case: str, scripted: Any,
    ) -> None:
        """真实身份失效不是服务不可用：必须 401，且请求后五 ContextVar 无残留。"""
        # Arrange
        identity, _ = _identity_pair(9)
        reader = _ScriptedIdentityReader({9: scripted})
        settings = _install_probe_auth(monkeypatch, env="prod", reader=reader)
        stack = _probe_stack(settings)
        token = _mint_token(identity)
        # Act
        response = await _call_probe_entry(stack, token)
        # Assert
        assert response.status_code == 401, f"{case} 应 401，实际 {response.status_code}"
        assert response.json() == {"detail": "令牌身份已失效"}
        assert reader.calls == [9]
        assert _read_context_vars() == ANONYMOUS_CONTEXT

    # 作用：验证同一 JWT 在依赖恢复后必须 200，且身份准确、外层上下文不串线。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    async def test_recovery_reuses_same_jwt_with_accurate_identity(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """故障→恢复复用同一 JWT；请求内身份等于 JWT 声明，请求外身份保持原值。"""
        # Arrange
        identity, expected_body = _identity_pair(9)
        reader = _ScriptedIdentityReader({9: TimeoutError(TIMEOUT_DETAIL_MARKER)})
        settings = _install_probe_auth(monkeypatch, env="prod", reader=reader)
        stack = _probe_stack(settings)
        token = _mint_token(identity)
        # Act
        with auth.use_identity_context(_SENTINEL_IDENTITY):
            outage_response = await _call_probe_entry(stack, token)
            reader.script[9] = identity
            recovered_response = await _call_probe_entry(stack, token)
            outer_after = _read_context_vars()
        # Assert
        assert outage_response.status_code == 503
        assert recovered_response.status_code == 200
        assert recovered_response.json() == expected_body
        assert outer_after == _SENTINEL_CONTEXT
        assert reader.calls == [9, 9]

    # 作用：验证身份查询取消必须原样传播，不得被吞成 503/401 或 ERROR 日志。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    async def test_cancellation_propagates_without_error_log(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CancelledError 逃逸出完整中间件栈，且该路径零 ERROR。"""
        # Arrange
        identity, _ = _identity_pair(9)
        reader = _ScriptedIdentityReader({9: asyncio.CancelledError()})
        settings = _install_probe_auth(monkeypatch, env="prod", reader=reader)
        stack = _probe_stack(settings)
        token = _mint_token(identity)
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": PROTECTED_PATH,
            "raw_path": PROTECTED_PATH.encode("ascii"), "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 80),
        }

        # 作用：接收通道替身；取消场景不得读取请求体。
        # Args: 无。
        # Returns: 空的 http.request 消息。
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        # 作用：发送通道替身；取消场景不得发送任何响应。
        # Args: message - ASGI 响应消息。
        # Returns: 无返回值；被调用即失败。
        async def send(message: dict[str, Any]) -> None:
            raise AssertionError(f"取消路径不应发送响应: {message}")

        # Act / Assert
        with capture_logs() as logs:
            with pytest.raises(asyncio.CancelledError):
                await stack(scope, receive, send)
        assert [entry for entry in logs if entry.get("log_level") == "error"] == []
        assert reader.calls == [9]
        assert _read_context_vars() == ANONYMOUS_CONTEXT


class TestAuthDependencyRawHttp:
    """36.2：回环真实 HTTP 验收（uvicorn 随机端口 + curl.exe，503→恢复 200）。"""

    # 作用：验证原始 TCP HTTP 下依赖超时 503，恢复后同一 JWT 200 且身份准确。
    # Args: self - 测试实例；monkeypatch - 补丁夹具；tmp_path - 响应体落盘目录。
    # Returns: 无返回值；只记录状态码与合成身份字段，不输出 JWT/请求头。
    @pytest.mark.integration
    async def test_raw_http_503_then_recovery_200(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """只连本测试启动的 127.0.0.1 随机端口，不访问任何用户服务端口。"""
        # Arrange
        identity, expected_body = _identity_pair(9)
        reader = _ScriptedIdentityReader({9: TimeoutError(TIMEOUT_DETAIL_MARKER)})
        settings = _install_probe_auth(monkeypatch, env="prod", reader=reader)
        stack = _probe_stack(settings)
        token = _mint_token(identity)
        server = _LoopbackUvicornServer(stack)
        await server.start()
        try:
            url = f"http://127.0.0.1:{server.port}{PROTECTED_PATH}"
            # Act：故障阶段
            outage_status, transport = await asyncio.to_thread(
                _raw_status_loopback, url, token,
            )
            # Assert：故障阶段为 503，且只走 curl.exe
            assert transport == "curl.exe"
            assert outage_status == 503, f"原始 HTTP 超时应为 503，实际 {outage_status}"
            # Act：依赖恢复后复用同一 JWT
            reader.script[9] = identity
            body_path = tmp_path / "recovery-body.json"
            recovered_status, _ = await asyncio.to_thread(
                _raw_status_loopback, url, token, body_path.as_posix(),
            )
            # Assert：恢复阶段 200 且身份准确
            assert recovered_status == 200
            assert json.loads(body_path.read_text(encoding="utf-8")) == expected_body
        finally:
            server.stop()
        assert reader.calls == [9, 9]
        assert _read_context_vars() == ANONYMOUS_CONTEXT
