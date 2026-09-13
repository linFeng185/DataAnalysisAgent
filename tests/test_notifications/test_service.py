"""通知服务测试：notify_user、发布权限、投递范围、已读删除与渠道配置。

覆盖契约（spec/48 第 7.5 节与模块交付说明）：
* notify_user 产生一条个人通知 + 一条收件人行；
* 四类通知的发布权限矩阵；
* audience 三种范围的展开规则（含跨租户 ID 剔除）；
* 已读 / 未读计数 / 软删；
* 租户渠道配置默认值与敏感值加密回显。
"""

from __future__ import annotations

import pytest

from src.notifications.models import ChannelSetting, ChannelsPayload, NotificationAudience
from src.notifications.service import (
    create_notification,
    delete_notification,
    get_channels,
    list_inbox,
    mark_all_read,
    mark_read,
    notify_user,
    save_channels,
    unread_count,
)
from tests.test_notifications.conftest import make_identity
from tests.test_notifications.fakes import BoomDispatcher, BoomHub, FakeWebSocket


class TestNotifyUser:
    """覆盖「notify_user 产生一条个人通知 + 一条收件人」契约。"""

    # 方法作用：验证个人通知落库后立即进入收件箱并计入未读。
    # Args: self - 测试实例；notification_runtime - 注入的 Store/Hub/分发器。
    # Returns: 无返回值。
    async def test_notify_user_creates_personal_notification_and_recipient(self, notification_runtime) -> None:
        """个人通知必须有且只有一条主体行和一条收件人行。"""
        # Arrange
        store = notification_runtime.store
        identity = make_identity()

        # Act
        await notify_user(3, 7, "personal", "报表已完成", "正文内容", "/report-workbench/jobs/1")

        # Assert
        assert len(store._notifications) == 1
        assert len(store._recipients) == 1
        inbox = await list_inbox(identity)
        assert inbox["total"] == 1
        item = inbox["items"][0]
        assert item["kind"] == "personal"
        assert item["title"] == "报表已完成"
        assert item["link"] == "/report-workbench/jobs/1"
        assert item["is_read"] is False
        assert await unread_count(identity) == 1

    # 方法作用：验证推送负载只含标识与摘要，不含正文。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_notify_user_push_payload_excludes_body(self, notification_runtime) -> None:
        """推送只用于角标与提示，不得携带业务正文。"""
        # Arrange
        socket = FakeWebSocket()
        await notification_runtime.hub.register(3, 7, socket)

        # Act
        await notify_user(3, 7, "personal", "标题", "机密正文", "/link")

        # Assert
        assert len(socket.messages) == 1
        payload = socket.messages[0]
        assert payload["type"] == "notification"
        assert payload["title"] == "标题"
        assert payload["unread_count"] == 1
        assert "body" not in payload
        assert "机密正文" not in str(payload)

    # 方法作用：验证 Hub 推送异常时通知仍然落库（推送降级）。
    # Args: self - 测试实例；monkeypatch - 补丁工具；notification_store - 内存 Store。
    # Returns: 无返回值。
    async def test_notify_user_survives_hub_failure(self, monkeypatch, notification_store) -> None:
        """推送失败只能记 warning，不得让通知发布抛异常或丢失。"""
        # Arrange
        monkeypatch.setattr("src.notifications.service.get_notification_store", lambda: notification_store)
        monkeypatch.setattr("src.notifications.service.get_notification_hub", lambda: BoomHub())
        monkeypatch.setattr("src.notifications.service.get_notification_dispatcher", lambda: BoomDispatcher())

        # Act
        await notify_user(3, 7, "personal", "标题", "正文")

        # Assert
        inbox = await notification_store.list_inbox(make_identity())
        assert inbox["total"] == 1

    # 方法作用：验证推送与渠道分发同时故障时通知仍落库。
    # Args: self - 测试实例；monkeypatch - 补丁工具；notification_store - 内存 Store；
    #       notification_hub - 正常 Hub。
    # Returns: 无返回值。
    async def test_notify_user_survives_dispatcher_failure(self, monkeypatch, notification_store, notification_hub) -> None:
        """渠道分发接缝异常同样不得影响站内信必达。"""
        # Arrange
        monkeypatch.setattr("src.notifications.service.get_notification_store", lambda: notification_store)
        monkeypatch.setattr("src.notifications.service.get_notification_hub", lambda: notification_hub)
        monkeypatch.setattr("src.notifications.service.get_notification_dispatcher", lambda: BoomDispatcher())

        # Act
        await notify_user(3, 7, "personal", "标题", "正文")

        # Assert
        inbox = await notification_store.list_inbox(make_identity())
        assert inbox["total"] == 1

    # 方法作用：验证未知通知类型回退为 personal 而不是写入非法枚举。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_notify_user_invalid_kind_falls_back_to_personal(self, notification_runtime) -> None:
        """业务代码传错类型时按个人通知处理，避免触发 CHECK 约束错误。"""
        # Arrange / Act
        await notify_user(3, 7, "unknown-kind", "标题", "正文")

        # Assert
        inbox = await notification_runtime.store.list_inbox(make_identity())
        assert inbox["items"][0]["kind"] == "personal"


class TestPublishPermissions:
    """覆盖四类通知的权限矩阵。"""

    # 方法作用：验证超管只能发布平台通知。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_super_admin_can_only_publish_platform(self, notification_runtime) -> None:
        """super_admin 发布租户/群通知必须 403（PermissionError）。"""
        # Arrange
        identity = make_identity(tenant_id=1, user_id=1, role="super_admin")

        # Act / Assert
        for kind in ("tenant", "group"):
            with pytest.raises(PermissionError):
                await create_notification(identity, kind, "标题", "正文")
        notification_id, delivered = await create_notification(
            identity, "platform", "平台公告", "正文", "", NotificationAudience(scope="all")
        )
        assert notification_id is not None
        assert delivered == 3

    # 方法作用：验证租户管理员只能发布租户通知与用户群通知。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_tenant_admin_can_only_publish_tenant_and_group(self, notification_runtime) -> None:
        """tenant_admin 发布平台通知必须 403。"""
        # Arrange
        identity = make_identity(role="tenant_admin")

        # Act / Assert
        with pytest.raises(PermissionError):
            await create_notification(identity, "platform", "平台公告", "正文")
        _, delivered = await create_notification(identity, "tenant", "租户公告", "正文")
        assert delivered == 2

    # 方法作用：验证 analyst 不能发布任何一类通知。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_analyst_cannot_publish_any_kind(self, notification_runtime) -> None:
        """非管理员角色对三类可发布通知都必须 403。"""
        # Arrange
        identity = make_identity(user_id=8, role="analyst")

        # Act / Assert
        for kind in ("platform", "tenant", "group"):
            with pytest.raises(PermissionError):
                await create_notification(identity, kind, "标题", "正文")


class TestAudienceExpansion:
    """覆盖 audience 三种范围的展开规则。"""

    # 方法作用：验证平台通知按租户各写一条主体行。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_platform_notification_writes_one_row_per_tenant(self, notification_runtime) -> None:
        """平台通知不做跨租户可见行，而是每个租户各写一条。"""
        # Arrange
        identity = make_identity(tenant_id=1, user_id=1, role="super_admin")

        # Act
        await create_notification(identity, "platform", "平台公告", "正文", "", NotificationAudience(scope="all"))

        # Assert
        rows = list(notification_runtime.store._notifications.values())
        assert sorted(row["tenant_id"] for row in rows) == [3, 4]

    # 方法作用：验证 all 范围展开为租户内全部启用用户。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_all_scope_expands_tenant_users(self, notification_runtime) -> None:
        """tenant 通知只发给当前租户的启用用户。"""
        # Arrange
        identity = make_identity(role="tenant_admin")

        # Act
        _, delivered = await create_notification(identity, "tenant", "租户公告", "正文")

        # Assert
        assert delivered == 2
        recipients = {user_id for (_, user_id) in notification_runtime.store._recipients}
        assert recipients == {7, 8}

    # 方法作用：验证 users 范围剔除跨租户与不存在的用户 ID。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_users_scope_ignores_cross_tenant_and_unknown_ids(self, notification_runtime) -> None:
        """user_ids 里的其它租户用户（9）与不存在的 ID 都不计入投递。"""
        # Arrange
        identity = make_identity(role="tenant_admin")
        audience = NotificationAudience(scope="users", user_ids=[7, 8, 9, 404])

        # Act
        _, delivered = await create_notification(identity, "group", "定向通知", "正文", "", audience)

        # Assert
        assert delivered == 2
        recipients = {user_id for (_, user_id) in notification_runtime.store._recipients}
        assert recipients == {7, 8}

    # 方法作用：验证 role 范围按角色展开。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_role_scope_expands_by_role(self, notification_runtime) -> None:
        """roles 命中 analyst 时只投递给 analyst。"""
        # Arrange
        identity = make_identity(role="tenant_admin")
        audience = NotificationAudience(scope="role", roles=["analyst"])

        # Act
        _, delivered = await create_notification(identity, "group", "分析员通知", "正文", "", audience)

        # Assert
        assert delivered == 1
        recipients = {user_id for (_, user_id) in notification_runtime.store._recipients}
        assert recipients == {8}

    # 方法作用：验证没有命中收件人时不写通知主体。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_empty_recipients_skips_write(self, notification_runtime) -> None:
        """空投递不应留下无收件人的孤儿通知。"""
        # Arrange
        identity = make_identity(role="tenant_admin")
        audience = NotificationAudience(scope="role", roles=["viewer"])

        # Act
        notification_id, delivered = await create_notification(identity, "group", "没人看", "正文", "", audience)

        # Assert
        assert notification_id is None
        assert delivered == 0
        assert notification_runtime.store._notifications == {}


class TestInboxLifecycle:
    """覆盖已读、未读计数与软删语义。"""

    # 方法作用：验证标记已读后未读数下降。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_mark_read_decreases_unread_count(self, notification_runtime) -> None:
        """已读是收件人维度的状态，只影响本人未读数。"""
        # Arrange
        await notify_user(3, 7, "personal", "标题", "正文")
        identity = make_identity()
        inbox = await list_inbox(identity)
        notification_id = inbox["items"][0]["id"]

        # Act
        matched = await mark_read(identity, notification_id)

        # Assert
        assert matched is True
        assert await unread_count(identity) == 0
        assert (await list_inbox(identity, unread_only=True))["total"] == 0

    # 方法作用：验证全部已读只作用于当前用户。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_mark_all_read_is_user_scoped(self, notification_runtime) -> None:
        """mark_all_read 不能顺带清掉别人的未读。"""
        # Arrange
        await notify_user(3, 7, "personal", "给7", "正文")
        await notify_user(3, 8, "personal", "给8", "正文")

        # Act
        updated = await mark_all_read(make_identity())

        # Assert
        assert updated == 1
        assert await unread_count(make_identity(user_id=8, role="analyst")) == 1

    # 方法作用：验证软删后不再出现在收件箱且未读数下降。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_delete_removes_from_inbox_and_unread(self, notification_runtime) -> None:
        """删除是软删：收件人行保留但带 deleted_at。"""
        # Arrange
        await notify_user(3, 7, "personal", "标题", "正文")
        identity = make_identity()
        notification_id = (await list_inbox(identity))["items"][0]["id"]

        # Act
        deleted = await delete_notification(identity, notification_id)

        # Assert
        assert deleted is True
        assert (await list_inbox(identity))["total"] == 0
        assert await unread_count(identity) == 0
        recipient = notification_runtime.store._recipients[(notification_id, 7)]
        assert recipient["deleted_at"] is not None
        assert len(notification_runtime.store._notifications) == 1

    # 方法作用：验证其他用户不能操作不属于自己的通知。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_other_user_cannot_touch_notification(self, notification_runtime) -> None:
        """跨用户标记已读与删除都必须未命中。"""
        # Arrange
        await notify_user(3, 7, "personal", "标题", "正文")
        notification_id = (await list_inbox(make_identity()))["items"][0]["id"]
        other = make_identity(user_id=8, role="analyst")

        # Act / Assert
        assert await mark_read(other, notification_id) is False
        assert await delete_notification(other, notification_id) is False


class TestChannelConfig:
    """覆盖租户级渠道配置的默认值、回显脱敏与权限。"""

    # 方法作用：验证默认只有站内信启用。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_default_channels_only_in_app_enabled(self, notification_runtime) -> None:
        """未配置过的租户必须得到 in_app=True、其余三个 False。"""
        # Act
        result = await get_channels(3)

        # Assert
        by_channel = {row["channel"]: row for row in result["channels"]}
        assert set(by_channel) == {"in_app", "email", "feishu", "slack"}
        assert by_channel["in_app"]["enabled"] is True
        assert all(by_channel[name]["enabled"] is False for name in ("email", "feishu", "slack"))

    # 方法作用：验证保存后读回一致且敏感值不回显明文。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_save_channels_roundtrip_masks_secret(self, notification_runtime) -> None:
        """SMTP 口令与 Webhook 只回显 xxx_configured。"""
        # Arrange
        payload = ChannelsPayload(
            channels={
                "email": ChannelSetting(
                    enabled=True,
                    config={"smtp_host": "smtp.example.com", "smtp_password": "top-secret"},
                ),
                "slack": ChannelSetting(enabled=False, config={"webhook_url": "https://hooks.example.com/x"}),
            }
        )
        identity = make_identity(role="tenant_admin")

        # Act
        saved = await save_channels(identity, payload)

        # Assert
        by_channel = {row["channel"]: row for row in saved["channels"]}
        assert by_channel["email"]["enabled"] is True
        assert by_channel["email"]["config"]["smtp_host"] == "smtp.example.com"
        assert by_channel["email"]["config"]["smtp_password_configured"] is True
        assert "top-secret" not in str(saved)
        assert by_channel["slack"]["config"]["webhook_url_configured"] is True
        assert "hooks.example.com" not in str(saved)
        # 密文确实入库，且不再是明文
        stored = notification_runtime.store._channels[(3, "email")]["config"]["smtp_password"]
        assert stored != "top-secret"
        assert stored.startswith("v")

    # 方法作用：验证 analyst 与 super_admin 都不能保存渠道配置。
    # Args: self - 测试实例；notification_runtime - 注入的运行时。
    # Returns: 无返回值。
    async def test_non_tenant_admin_cannot_save_channels(self, notification_runtime) -> None:
        """渠道配置仅 tenant_admin 可写（超管 403，与报表额度口径一致）。"""
        # Arrange
        payload = ChannelsPayload(channels={"in_app": ChannelSetting(enabled=True, config={})})

        # Act / Assert
        for identity in (make_identity(user_id=8, role="analyst"), make_identity(user_id=1, role="super_admin")):
            with pytest.raises(PermissionError):
                await save_channels(identity, payload)

    # 方法作用：验证未知渠道名在模型层被拒绝。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_unknown_channel_is_rejected(self) -> None:
        """渠道白名单以外的键必须触发校验错误。"""
        # Arrange / Act / Assert
        with pytest.raises(ValueError):
            ChannelsPayload(channels={"sms": ChannelSetting(enabled=True, config={})})
