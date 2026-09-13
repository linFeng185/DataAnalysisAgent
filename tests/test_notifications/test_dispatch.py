"""通知渠道分发接缝测试：默认不发、未配置放行、注入发送器成败隔离。"""

from __future__ import annotations

from src.notifications.dispatch import NotificationChannelDispatcher


# 方法作用：把某渠道置为启用并写入最小配置。
# Args: store - 内存 Store；channel - 渠道名；config - 渠道配置。
# Returns: 无返回值。
async def _enable_channel(store, channel: str, config: dict | None = None) -> None:
    await store.save_channels(3, {channel: {"enabled": True, "config": config or {}}}, 0)


class TestNotificationChannelDispatcher:
    """覆盖需求 1.9「外部渠道先不发」与降级语义。"""

    # 方法作用：验证默认配置下外部渠道全部跳过。
    # Args: self - 测试实例；notification_store - 内存 Store。
    # Returns: 无返回值。
    async def test_default_channels_are_disabled(self, notification_store) -> None:
        """未启用时状态为 disabled，且不构造任何发送器。"""
        # Arrange
        dispatcher = NotificationChannelDispatcher(store=notification_store)

        # Act
        statuses = await dispatcher.dispatch(3, {"id": "n1", "title": "标题", "body": "正文"}, [7])

        # Assert
        assert statuses == {"in_app": "success", "email": "disabled", "feishu": "disabled", "slack": "disabled"}

    # 方法作用：验证启用但未提供发送实现时记 not_configured 并放行。
    # Args: self - 测试实例；notification_store - 内存 Store。
    # Returns: 无返回值。
    async def test_enabled_without_sender_is_not_configured(self, notification_store) -> None:
        """本次不实现真实发送，状态必须是 not_configured 而不是假成功。"""
        # Arrange
        await _enable_channel(notification_store, "email", {"smtp_host": "smtp.example.com"})
        dispatcher = NotificationChannelDispatcher(store=notification_store)

        # Act
        statuses = await dispatcher.dispatch(3, {"id": "n1", "title": "标题", "body": "正文"}, [7])

        # Assert
        assert statuses["email"] == "not_configured"
        assert statuses["in_app"] == "success"

    # 方法作用：验证注入发送器成功时状态为 success。
    # Args: self - 测试实例；notification_store - 内存 Store。
    # Returns: 无返回值。
    async def test_injected_sender_success(self, notification_store) -> None:
        """注入发送器收到标题与正文。"""
        # Arrange
        await _enable_channel(notification_store, "email", {"email_from": "bot@example.com"})
        calls: list[tuple[str, str, str]] = []

        # 方法作用：记录一次发送调用。
        # Args: recipient - 收件目标；title - 标题；body - 正文。
        # Returns: 无返回值。
        async def fake_sender(recipient: str, title: str, body: str) -> None:
            calls.append((recipient, title, body))

        dispatcher = NotificationChannelDispatcher(store=notification_store, senders={"email": fake_sender})

        # Act
        statuses = await dispatcher.dispatch(3, {"id": "n1", "title": "标题", "body": "正文"}, [7])

        # Assert
        assert statuses["email"] == "success"
        assert calls == [("bot@example.com", "标题", "正文")]

    # 方法作用：验证单个渠道失败不影响其它渠道与站内信。
    # Args: self - 测试实例；notification_store - 内存 Store。
    # Returns: 无返回值。
    async def test_sender_failure_is_isolated(self, notification_store) -> None:
        """email 抛异常时 feishu 仍照常尝试，站内信保持 success。"""
        # Arrange
        await _enable_channel(notification_store, "email")
        await _enable_channel(notification_store, "feishu", {"webhook_url": "v2:salt:token"})
        sent: list[str] = []

        # 方法作用：模拟失败与成功的两类发送器。
        # Args: recipient - 收件目标；title - 标题；body - 正文。
        # Returns: 无返回值。
        async def boom_sender(recipient: str, title: str, body: str) -> None:
            del recipient, title, body
            raise RuntimeError("smtp down")

        # 方法作用：记录成功的发送调用。
        # Args: recipient - 收件目标；title - 标题；body - 正文。
        # Returns: 无返回值。
        async def ok_sender(recipient: str, title: str, body: str) -> None:
            del title, body
            sent.append(recipient)

        dispatcher = NotificationChannelDispatcher(
            store=notification_store,
            senders={"email": boom_sender, "feishu": ok_sender},
        )

        # Act
        statuses = await dispatcher.dispatch(3, {"id": "n1", "title": "标题", "body": "正文"}, [7])

        # Assert
        assert statuses["email"] == "error"
        assert statuses["feishu"] == "success"
        assert statuses["in_app"] == "success"
        assert len(sent) == 1

    # 方法作用：验证渠道配置读取失败时只保留站内信状态。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_store_failure_falls_back_to_in_app_only(self) -> None:
        """配置读取异常不能让通知发布失败。"""
        # Arrange
        class BrokenStore:
            """读取渠道配置必失败的 Store 替身。"""

            # 方法作用：模拟配置读取故障。
            # Args: tenant_id - 租户 ID。
            # Returns: 无返回值；始终抛出 RuntimeError。
            async def get_channels(self, tenant_id: int) -> list[dict]:
                del tenant_id
                raise RuntimeError("db down")

        dispatcher = NotificationChannelDispatcher(store=BrokenStore())

        # Act
        statuses = await dispatcher.dispatch(3, {"id": "n1", "title": "标题", "body": "正文"}, [7])

        # Assert
        assert statuses == {"in_app": "success"}
