"""通知 WebSocket 连接管理：进程内字典 + asyncio 锁。

多 Worker 边界（重要）：
    本 Hub 只保存「当前进程」持有的连接，broadcast_to_user 也只推送本进程
    的连接集合。多 Worker 部署时，用户可能连在另一个 Worker 上，此时
    站内信仍然落库（收件箱可见），但实时推送不会到达，需要外接
    Redis Pub/Sub 等跨进程通道才能补齐。本次不引入新依赖，仅保证
    单进程内多标签页推送与断线清理正确。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)

# 连接集合的键：租户 + 用户；同一用户允许多个标签页连接。
ConnectionKey = tuple[int, int]


class NotificationHub:
    """维护 (tenant_id, user_id) 到 WebSocket 连接集合的映射。"""

    # 方法作用：初始化空连接表与并发锁。
    # Args: self - Hub 实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self._connections: dict[ConnectionKey, list[Any]] = {}
        self._lock = asyncio.Lock()
        logger.debug("通知 Hub 初始化完成")

    # 方法作用：把一条已通过鉴权的 WebSocket 连接加入用户连接集合。
    # Args: self - Hub 实例；tenant_id - 租户 ID；user_id - 用户 ID；websocket - 已接受的连接对象。
    # Returns: 加入后该用户的连接总数。
    async def register(self, tenant_id: int, user_id: int, websocket: Any) -> int:
        key = (int(tenant_id), int(user_id))
        async with self._lock:
            connections = self._connections.setdefault(key, [])
            connections.append(websocket)
            total = len(connections)
        logger.info(
            "通知 WebSocket 连接注册完成",
            tenant_id=key[0],
            user_id=key[1],
            connection_count=total,
        )
        return total

    # 方法作用：从用户连接集合移除一条连接，空集合直接丢弃键。
    # Args: self - Hub 实例；tenant_id - 租户 ID；user_id - 用户 ID；websocket - 断开的连接对象。
    # Returns: 无返回值。
    async def unregister(self, tenant_id: int, user_id: int, websocket: Any) -> None:
        key = (int(tenant_id), int(user_id))
        async with self._lock:
            connections = self._connections.get(key)
            if connections is not None:
                self._connections[key] = [
                    item for item in connections if item is not websocket
                ]
                if not self._connections[key]:
                    del self._connections[key]
            remaining = len(self._connections.get(key, []))
        logger.info(
            "通知 WebSocket 连接注销完成",
            tenant_id=key[0],
            user_id=key[1],
            connection_count=remaining,
        )

    # 方法作用：把推送负载发送给某用户的全部在线连接，并清理发送失败的断线连接。
    # Args: self - Hub 实例；tenant_id - 租户 ID；user_id - 用户 ID；payload - 只含标识与摘要的 JSON 字典。
    # Returns: 实际推送成功的连接数。
    async def broadcast_to_user(self, tenant_id: int, user_id: int, payload: dict[str, Any]) -> int:
        key = (int(tenant_id), int(user_id))
        logger.debug(
            "通知推送入口",
            tenant_id=key[0],
            user_id=key[1],
            payload_keys=sorted(payload),
        )
        async with self._lock:
            connections = list(self._connections.get(key, []))
        if not connections:
            logger.info(
                "通知推送跳过",
                tenant_id=key[0],
                user_id=key[1],
                reason="当前进程无在线连接",
            )
            return 0

        sent = 0
        stale: list[Any] = []
        for websocket in connections:
            try:
                await self._send(websocket, payload)
                sent += 1
            except Exception as exc:
                stale.append(websocket)
                logger.warning(
                    "通知推送失败，连接将被移除",
                    tenant_id=key[0],
                    user_id=key[1],
                    error_type=type(exc).__name__,
                )
        if stale:
            async with self._lock:
                current = self._connections.get(key)
                if current is not None:
                    self._connections[key] = [
                        item for item in current if item not in stale
                    ]
                    if not self._connections[key]:
                        del self._connections[key]
        logger.info(
            "通知推送完成",
            tenant_id=key[0],
            user_id=key[1],
            sent=sent,
            stale=len(stale),
        )
        return sent

    # 方法作用：列出指定租户下当前进程有连接的用户 ID，用于只给在线用户构造推送负载。
    # Args: self - Hub 实例；tenant_id - 租户 ID。
    # Returns: 去重后的用户 ID 列表。
    async def online_user_ids(self, tenant_id: int) -> list[int]:
        async with self._lock:
            return sorted(
                user_id
                for (key_tenant, key_user), items in self._connections.items()
                if key_tenant == int(tenant_id) and items
                for user_id in (key_user,)
            )

    # 方法作用：统计在线连接数，供健康检查与测试断言使用。
    # Args: self - Hub 实例；tenant_id - 可选租户过滤；user_id - 可选用户过滤。
    # Returns: 命中过滤条件的连接数。
    async def connection_count(self, tenant_id: int | None = None, user_id: int | None = None) -> int:
        async with self._lock:
            if tenant_id is None and user_id is None:
                return sum(len(items) for items in self._connections.values())
            total = 0
            for (key_tenant, key_user), items in self._connections.items():
                if tenant_id is not None and key_tenant != int(tenant_id):
                    continue
                if user_id is not None and key_user != int(user_id):
                    continue
                total += len(items)
            return total

    # 方法作用：把一条推送消息发送给单个连接，兼容 send_json 与 send_text 两种实现。
    # Args: self - Hub 实例；websocket - 目标连接；payload - JSON 字典。
    # Returns: 无返回值；发送失败时抛出底层异常由调用方清理。
    @staticmethod
    async def _send(websocket: Any, payload: dict[str, Any]) -> None:
        send_json = getattr(websocket, "send_json", None)
        if callable(send_json):
            await send_json(payload)
            return
        import json

        send_text = getattr(websocket, "send_text", None)
        if not callable(send_text):
            raise TypeError("WebSocket 连接缺少 send_json/send_text 方法")
        await send_text(json.dumps(payload, ensure_ascii=False))


_hub: NotificationHub | None = None


# 方法作用：获取进程级唯一通知 Hub。
# Args: 无。
# Returns: 当前进程共享的 NotificationHub 实例。
def get_notification_hub() -> NotificationHub:
    global _hub
    if _hub is None:
        _hub = NotificationHub()
    return _hub


# 方法作用：用注入的 Hub 替换进程级实例，供测试与运行时装配使用。
# Args: hub - 新的 Hub 实例；None 表示恢复惰性创建。
# Returns: 无返回值。
def set_notification_hub(hub: NotificationHub | None) -> None:
    global _hub
    _hub = hub
    logger.info("通知 Hub 已替换", injected=hub is not None)
