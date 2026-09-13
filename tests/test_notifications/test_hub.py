"""通知 Hub 测试：连接注册、注销、多标签页广播与断线清理。

不建立真实 WebSocket 连接，全部使用 FakeWebSocket 直接驱动 Hub。
"""

from __future__ import annotations

from src.notifications.hub import NotificationHub
from tests.test_notifications.fakes import FakeWebSocket


class TestNotificationHub:
    """覆盖 hub 的连接生命周期与推送语义。"""

    # 方法作用：验证同一用户多个标签页的连接都被登记。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_register_tracks_multiple_tabs(self) -> None:
        """register 返回该用户当前连接数。"""
        # Arrange
        hub = NotificationHub()
        first, second = FakeWebSocket(), FakeWebSocket()

        # Act
        first_count = await hub.register(3, 7, first)
        second_count = await hub.register(3, 7, second)

        # Assert
        assert first_count == 1
        assert second_count == 2
        assert await hub.connection_count(3, 7) == 2

    # 方法作用：验证注销只移除目标连接。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_unregister_only_removes_target(self) -> None:
        """注销一个标签页不能影响同用户其它连接。"""
        # Arrange
        hub = NotificationHub()
        first, second = FakeWebSocket(), FakeWebSocket()
        await hub.register(3, 7, first)
        await hub.register(3, 7, second)

        # Act
        await hub.unregister(3, 7, first)

        # Assert
        assert await hub.connection_count(3, 7) == 1

    # 方法作用：验证最后一个连接注销后键被清理。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_unregister_last_connection_clears_key(self) -> None:
        """空集合不应残留空列表键。"""
        # Arrange
        hub = NotificationHub()
        socket = FakeWebSocket()
        await hub.register(3, 7, socket)

        # Act
        await hub.unregister(3, 7, socket)

        # Assert
        assert await hub.connection_count() == 0
        assert hub._connections == {}

    # 方法作用：验证广播送达同用户的全部标签页。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_broadcast_reaches_all_tabs(self) -> None:
        """同一用户的多标签页都要收到同一条推送。"""
        # Arrange
        hub = NotificationHub()
        first, second, stranger = FakeWebSocket(), FakeWebSocket(), FakeWebSocket()
        await hub.register(3, 7, first)
        await hub.register(3, 7, second)
        await hub.register(3, 8, stranger)
        payload = {"type": "notification", "id": "n1", "unread_count": 2}

        # Act
        sent = await hub.broadcast_to_user(3, 7, payload)

        # Assert
        assert sent == 2
        assert first.messages == [payload]
        assert second.messages == [payload]
        assert stranger.messages == []

    # 方法作用：验证推送失败的连接被清理。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_broadcast_drops_stale_connection(self) -> None:
        """断线连接发送失败后必须移出集合，避免反复失败。"""
        # Arrange
        hub = NotificationHub()
        healthy, broken = FakeWebSocket(), FakeWebSocket(fail=True)
        await hub.register(3, 7, healthy)
        await hub.register(3, 7, broken)

        # Act
        sent = await hub.broadcast_to_user(3, 7, {"type": "notification"})

        # Assert
        assert sent == 1
        assert await hub.connection_count(3, 7) == 1
        assert broken not in hub._connections[(3, 7)]

    # 方法作用：验证没有在线连接时广播返回 0 且不报错。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_broadcast_without_connection_returns_zero(self) -> None:
        """离线用户的通知只落库，不推送。"""
        # Arrange
        hub = NotificationHub()

        # Act
        sent = await hub.broadcast_to_user(3, 7, {"type": "notification"})

        # Assert
        assert sent == 0

    # 方法作用：验证在线用户列表按租户过滤。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_online_user_ids_filters_tenant(self) -> None:
        """跨租户用户不能出现在同一租户的在线列表里。"""
        # Arrange
        hub = NotificationHub()
        await hub.register(3, 7, FakeWebSocket())
        await hub.register(3, 8, FakeWebSocket())
        await hub.register(4, 9, FakeWebSocket())

        # Act
        online = await hub.online_user_ids(3)

        # Assert
        assert online == [7, 8]
