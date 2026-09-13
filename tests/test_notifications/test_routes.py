"""通知 HTTP 接口与 WebSocket 握手测试。

HTTP 部分用进程内 ASGI transport（无真实网络）；WebSocket 只测握手鉴权函数，
连接注册/广播由 test_hub.py 直接对 Hub 单测，不建立真实连接。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.auth import create_access_token
from src.api.routes.notifications import authenticate_websocket
from src.notifications.dispatch import NotificationChannelDispatcher
from src.notifications.service import notify_user
from tests.test_notifications.conftest import make_identity
from tests.test_notifications.fakes import FakeWebSocket


@pytest.fixture
def identity_holder() -> dict:
    """可变的当前身份容器，供不同类型用例切换登录者。"""
    return {"identity": make_identity()}


@pytest.fixture
def notification_app(monkeypatch, notification_store, notification_hub, identity_holder):
    """只挂通知路由的最小 FastAPI 应用，避免启动完整应用与数据库。"""
    from fastapi import FastAPI

    from src.api.routes.notifications import router as notifications_router

    app = FastAPI()
    app.include_router(notifications_router, prefix="/api/v1")
    dispatcher = NotificationChannelDispatcher(store=notification_store)
    monkeypatch.setattr("src.notifications.service.get_notification_store", lambda: notification_store)
    monkeypatch.setattr("src.notifications.service.get_notification_hub", lambda: notification_hub)
    monkeypatch.setattr("src.notifications.service.get_notification_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        "src.api.routes.notifications.get_current_identity",
        lambda: identity_holder["identity"],
    )
    return app


@pytest.fixture
async def client(notification_app):
    """进程内 HTTP 客户端。"""
    async with AsyncClient(
        transport=ASGITransport(app=notification_app),
        base_url="http://test",
    ) as http_client:
        yield http_client


class TestInboxRoutes:
    """覆盖收件箱、未读计数与分页夹紧。"""

    # 方法作用：验证 page_size 超出上限时被夹紧而不是报错。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_inbox_clamps_page_size(self, client) -> None:
        """page_size=1000 必须夹紧到 100 并保持 200。"""
        # Arrange
        await notify_user(3, 7, "personal", "标题", "正文")

        # Act
        response = await client.get("/api/v1/notifications/inbox", params={"page_size": 1000})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["page_size"] == 100
        assert body["total"] == 1

    # 方法作用：验证 page_size 为 0 时夹紧为 1。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_inbox_clamps_lower_bound(self, client) -> None:
        """非法小值同样夹紧，不返回 422。"""
        # Act
        response = await client.get("/api/v1/notifications/inbox", params={"page": 0, "page_size": 0})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["page"] == 1
        assert body["page_size"] == 1

    # 方法作用：验证未读计数接口返回 count。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_unread_count_endpoint(self, client) -> None:
        """未读数在已读后归零。"""
        # Arrange
        await notify_user(3, 7, "personal", "标题", "正文")
        notification_id = (await client.get("/api/v1/notifications/inbox")).json()["items"][0]["id"]

        # Act
        before = await client.get("/api/v1/notifications/unread-count")
        read = await client.post(f"/api/v1/notifications/{notification_id}/read")
        after = await client.get("/api/v1/notifications/unread-count")

        # Assert
        assert before.json() == {"count": 1}
        assert read.status_code == 200
        assert after.json() == {"count": 0}

    # 方法作用：验证 read-all 与软删接口。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_read_all_and_delete(self, client) -> None:
        """全部已读后删除只剩 deleted 标记，收件箱为空。"""
        # Arrange
        await notify_user(3, 7, "personal", "标题", "正文")
        notification_id = (await client.get("/api/v1/notifications/inbox")).json()["items"][0]["id"]

        # Act
        read_all = await client.post("/api/v1/notifications/read-all")
        deleted = await client.delete(f"/api/v1/notifications/{notification_id}")
        inbox = await client.get("/api/v1/notifications/inbox")

        # Assert
        assert read_all.json() == {"updated": 1}
        assert deleted.status_code == 200
        assert inbox.json()["total"] == 0


class TestRouteErrors:
    """覆盖 404 与 422 边界。"""

    # 方法作用：验证不存在的通知 ID 返回 404。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_missing_notification_returns_404(self, client) -> None:
        """read 与 delete 对未知 ID 都必须 404。"""
        # Arrange
        missing = str(uuid.uuid4())

        # Act
        read = await client.post(f"/api/v1/notifications/{missing}/read")
        deleted = await client.delete(f"/api/v1/notifications/{missing}")

        # Assert
        assert read.status_code == 404
        assert deleted.status_code == 404

    # 方法作用：验证非法 UUID 按不存在处理。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_invalid_uuid_returns_404(self, client) -> None:
        """非法标识不能触发数据库转型错误（500）。"""
        # Act
        response = await client.post("/api/v1/notifications/not-a-uuid/read")

        # Assert
        assert response.status_code == 404

    # 方法作用：验证空标题返回 422。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_publish_blank_title_returns_422(self, client) -> None:
        """title 只有空白字符时必须 422。"""
        # Act
        response = await client.post(
            "/api/v1/notifications/publish",
            json={"kind": "tenant", "title": "   ", "body": "正文"},
        )

        # Assert
        assert response.status_code == 422

    # 方法作用：验证 users 范围缺少 user_ids 返回 422。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_publish_users_scope_requires_user_ids(self, client) -> None:
        """scope=users 且 user_ids 为空时必须 422。"""
        # Act
        response = await client.post(
            "/api/v1/notifications/publish",
            json={
                "kind": "group",
                "title": "定向通知",
                "body": "正文",
                "audience": {"scope": "users", "user_ids": []},
            },
        )

        # Assert
        assert response.status_code == 422

    # 方法作用：验证 personal 类型不能从 /publish 发布。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_publish_personal_kind_returns_422(self, client) -> None:
        """个人通知只能由业务代码调用 notify_user 产生。"""
        # Act
        response = await client.post(
            "/api/v1/notifications/publish",
            json={"kind": "personal", "title": "标题", "body": "正文"},
        )

        # Assert
        assert response.status_code == 422


class TestRoutePermissions:
    """覆盖发布与渠道配置的角色权限。"""

    # 方法作用：验证 analyst 发布任何类型都 403。
    # Args: self - 测试实例；client - 进程内客户端；identity_holder - 身份容器。
    # Returns: 无返回值。
    async def test_analyst_publish_is_forbidden(self, client, identity_holder) -> None:
        """analyst 对三类通知都返回 403。"""
        # Arrange
        identity_holder["identity"] = make_identity(user_id=8, role="analyst")

        # Act / Assert
        for kind in ("platform", "tenant", "group"):
            response = await client.post(
                "/api/v1/notifications/publish",
                json={"kind": kind, "title": "标题", "body": "正文"},
            )
            assert response.status_code == 403

    # 方法作用：验证 tenant_admin 发布 tenant 通知成功。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_tenant_admin_publish_succeeds(self, client) -> None:
        """tenant 通知展开为当前租户的全部启用用户。"""
        # Act
        response = await client.post(
            "/api/v1/notifications/publish",
            json={"kind": "tenant", "title": "租户公告", "body": "正文"},
        )

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["delivered"] == 2
        assert body["notification_id"]

    # 方法作用：验证渠道配置读写仅租户管理员可用。
    # Args: self - 测试实例；client - 进程内客户端；identity_holder - 身份容器。
    # Returns: 无返回值。
    async def test_channels_admin_requires_tenant_admin(self, client, identity_holder) -> None:
        """analyst 与 super_admin 读写渠道配置都必须 403。"""
        # Arrange
        payload = {"channels": {"in_app": {"enabled": True, "config": {}}}}

        # Act / Assert
        for identity in (make_identity(user_id=8, role="analyst"), make_identity(user_id=1, role="super_admin")):
            identity_holder["identity"] = identity
            assert (await client.get("/api/v1/notifications/admin/channels")).status_code == 403
            assert (await client.put("/api/v1/notifications/admin/channels", json=payload)).status_code == 403

    # 方法作用：验证渠道配置保存与读取的完整链路。
    # Args: self - 测试实例；client - 进程内客户端。
    # Returns: 无返回值。
    async def test_channels_roundtrip(self, client) -> None:
        """保存后读回一致，且不出现明文口令。"""
        # Act
        saved = await client.put(
            "/api/v1/notifications/admin/channels",
            json={"channels": {"email": {"enabled": True, "config": {"smtp_password": "top-secret"}}}},
        )
        fetched = await client.get("/api/v1/notifications/admin/channels")

        # Assert
        assert saved.status_code == 200
        assert fetched.status_code == 200
        body = fetched.json()
        email = next(row for row in body["channels"] if row["channel"] == "email")
        assert email["enabled"] is True
        assert email["config"]["smtp_password_configured"] is True
        assert "top-secret" not in fetched.text


class TestWebSocketHandshake:
    """覆盖 WS 握手鉴权：不建立真实连接，只调用握手函数。"""

    # 方法作用：验证缺少令牌时拒绝握手。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_missing_token_is_rejected(self, monkeypatch) -> None:
        """无 Cookie 也无 Bearer 头时必须拒绝。"""
        # Arrange
        monkeypatch.setattr(
            "src.api.routes.notifications.get_settings",
            lambda: type("S", (), {"database_url": "", "env": "dev"})(),
        )
        socket = FakeWebSocket()

        # Act
        identity = await authenticate_websocket(socket)

        # Assert
        assert identity is None

    # 方法作用：验证 Cookie 中的有效令牌可以通过握手。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_valid_cookie_token_is_accepted(self, monkeypatch) -> None:
        """HttpOnly Cookie 是浏览器 WS 握手的身份来源。"""
        # Arrange
        monkeypatch.setattr(
            "src.api.routes.notifications.get_settings",
            lambda: type("S", (), {"database_url": "", "env": "dev"})(),
        )
        token = create_access_token(7, 3, "tenant_admin", "default", "alice")
        socket = FakeWebSocket()
        socket.cookies = {"access_token": token}

        # Act
        identity = await authenticate_websocket(socket)

        # Assert
        assert identity is not None
        assert identity[0] == 7
        assert identity[1] == 3
        assert identity[2] == "tenant_admin"

    # 方法作用：验证 Bearer 头也能通过握手（非浏览器客户端）。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_bearer_token_is_accepted(self, monkeypatch) -> None:
        """非浏览器客户端用 Authorization 头携带令牌。"""
        # Arrange
        monkeypatch.setattr(
            "src.api.routes.notifications.get_settings",
            lambda: type("S", (), {"database_url": "", "env": "dev"})(),
        )
        token = create_access_token(7, 3, "tenant_admin", "default", "alice")
        socket = FakeWebSocket()
        socket.headers = {"authorization": f"Bearer {token}"}

        # Act
        identity = await authenticate_websocket(socket)

        # Assert
        assert identity is not None
        assert identity[0] == 7

    # 方法作用：验证数据库复核发现身份停用时拒绝握手。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_stale_identity_is_rejected(self, monkeypatch) -> None:
        """账号停用后旧令牌不能再建立长连接。"""
        # Arrange
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "src.api.routes.notifications.get_settings",
            lambda: type("S", (), {"database_url": "postgresql://db/x", "env": "dev"})(),
        )
        monkeypatch.setattr(
            "src.api.routes.notifications.load_current_identity",
            AsyncMock(return_value=None),
        )
        token = create_access_token(7, 3, "tenant_admin", "default", "alice")
        socket = FakeWebSocket()
        socket.cookies = {"access_token": token}

        # Act
        identity = await authenticate_websocket(socket)

        # Assert
        assert identity is None

    # 方法作用：验证非法令牌被拒绝。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_invalid_token_is_rejected(self, monkeypatch) -> None:
        """伪造令牌必须拒绝。"""
        # Arrange
        monkeypatch.setattr(
            "src.api.routes.notifications.get_settings",
            lambda: type("S", (), {"database_url": "", "env": "dev"})(),
        )
        socket = FakeWebSocket()
        socket.cookies = {"access_token": "not-a-jwt"}

        # Act
        identity = await authenticate_websocket(socket)

        # Assert
        assert identity is None
