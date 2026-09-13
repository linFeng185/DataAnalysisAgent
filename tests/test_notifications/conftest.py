"""通知模块测试的共享装配：内存 Store、Hub 与运行时替身。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.notifications.dispatch import NotificationChannelDispatcher
from src.notifications.hub import NotificationHub
from src.notifications.store import NotificationStore


# 方法作用：构造请求身份替身，字段与 RequestIdentity 保持一致。
# Args: tenant_id - 租户 ID；user_id - 用户 ID；role - 角色。
# Returns: 可用于 service/route 的 SimpleNamespace 身份。
def make_identity(tenant_id: int = 3, user_id: int = 7, role: str = "tenant_admin") -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id, role=role)


@pytest.fixture
def notification_store() -> NotificationStore:
    """内存分支 Store：租户 3 有 tenant_admin(7) 与 analyst(8)，租户 4 有 analyst(9)。"""
    store = NotificationStore(memory=True)
    store.register_user(3, 7, "tenant_admin")
    store.register_user(3, 8, "analyst")
    store.register_user(4, 9, "analyst")
    return store


@pytest.fixture
def notification_hub() -> NotificationHub:
    """每个用例全新 Hub，避免连接残留互相污染。"""
    return NotificationHub()


@pytest.fixture
def notification_runtime(monkeypatch, notification_store, notification_hub):
    """把 service 的 Store / Hub / 分发器单例替换为测试替身。"""
    dispatcher = NotificationChannelDispatcher(store=notification_store)
    monkeypatch.setattr("src.notifications.service.get_notification_store", lambda: notification_store)
    monkeypatch.setattr("src.notifications.service.get_notification_hub", lambda: notification_hub)
    monkeypatch.setattr("src.notifications.service.get_notification_dispatcher", lambda: dispatcher)
    return SimpleNamespace(store=notification_store, hub=notification_hub, dispatcher=dispatcher)
