"""通知模块测试使用的假对象：不建立真实网络连接。"""

from __future__ import annotations

from typing import Any


class FakeWebSocket:
    """记录推送消息与关闭码的假 WebSocket 连接。"""

    # 方法作用：初始化假连接并记录是否模拟发送失败。
    # Args: fail - True 时 send_json 抛异常，模拟已断线的连接。
    # Returns: 无返回值。
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[dict[str, Any]] = []
        self.fail = fail
        self.closed: list[int] = []
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    # 方法作用：记录或拒绝一条推送消息。
    # Args: payload - 推送负载字典。
    # Returns: 无返回值；fail=True 时抛出 RuntimeError。
    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("连接已断开")
        self.messages.append(payload)

    # 方法作用：记录关闭码，供鉴权失败断言使用。
    # Args: code - WebSocket 关闭码。
    # Returns: 无返回值。
    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


class BoomHub:
    """在线但推送必失败的 Hub 替身，用于验证推送降级。"""

    # 方法作用：伪造一个在线用户，让服务层进入推送分支。
    # Args: tenant_id - 租户 ID。
    # Returns: 固定返回 [7]。
    async def online_user_ids(self, tenant_id: int) -> list[int]:
        del tenant_id
        return [7]

    # 方法作用：模拟推送通道故障。
    # Args: tenant_id/user_id/payload - 推送参数。
    # Returns: 无返回值；始终抛出 RuntimeError。
    async def broadcast_to_user(self, tenant_id: int, user_id: int, payload: dict[str, Any]) -> int:
        del tenant_id, user_id, payload
        raise RuntimeError("hub down")


class BoomDispatcher:
    """渠道分发必失败的替身，用于验证分发降级。"""

    # 方法作用：模拟渠道分发接缝故障。
    # Args: tenant_id/notification/user_ids - 分发参数。
    # Returns: 无返回值；始终抛出 RuntimeError。
    async def dispatch(
        self,
        tenant_id: int,
        notification: dict[str, Any],
        user_ids: list[int] | None = None,
    ) -> dict[str, str]:
        del tenant_id, notification, user_ids
        raise RuntimeError("channel down")
