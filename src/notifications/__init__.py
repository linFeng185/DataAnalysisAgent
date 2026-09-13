"""通用通知模块：四类通知、站内信收件箱、WebSocket 推送与租户级渠道配置。

其它模块产生个人通知统一调用：
    from src.notifications.service import notify_user
    await notify_user(tenant_id, user_id, "personal", "标题", "正文", "/link")
"""

from __future__ import annotations

from src.notifications.dispatch import (
    NotificationChannelDispatcher,
    get_notification_dispatcher,
    set_notification_dispatcher,
)
from src.notifications.hub import (
    NotificationHub,
    get_notification_hub,
    set_notification_hub,
)
from src.notifications.models import (
    AUDIENCE_SCOPES,
    CHANNEL_CONFIG_FIELDS,
    MAX_PAGE_SIZE,
    NOTIFICATION_CHANNELS,
    NOTIFICATION_KINDS,
    PUBLISHABLE_KINDS,
    SENSITIVE_CHANNEL_FIELDS,
    ChannelSetting,
    ChannelsPayload,
    NotificationAudience,
    NotificationPublishRequest,
)
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
from src.notifications.store import (
    NotificationStore,
    get_notification_store,
    set_notification_store,
)

__all__ = [
    "AUDIENCE_SCOPES",
    "CHANNEL_CONFIG_FIELDS",
    "MAX_PAGE_SIZE",
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_KINDS",
    "PUBLISHABLE_KINDS",
    "SENSITIVE_CHANNEL_FIELDS",
    "ChannelSetting",
    "ChannelsPayload",
    "NotificationAudience",
    "NotificationChannelDispatcher",
    "NotificationHub",
    "NotificationPublishRequest",
    "NotificationStore",
    "create_notification",
    "delete_notification",
    "get_channels",
    "get_notification_dispatcher",
    "get_notification_hub",
    "get_notification_store",
    "list_inbox",
    "mark_all_read",
    "mark_read",
    "notify_user",
    "save_channels",
    "set_notification_dispatcher",
    "set_notification_hub",
    "set_notification_store",
    "unread_count",
]
