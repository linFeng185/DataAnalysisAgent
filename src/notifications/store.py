"""通知持久化：PostgreSQL 生产分支与内存测试分支。

PostgreSQL 分支的所有写操作使用系统身份（super_admin）连接，因为一条通知要
同时写给多个收件人；读、已读、删除则使用当前请求身份连接，让 RLS 成为
tenant_id / user_id 隔离的最后一道防线（迁移 033）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.config import get_settings
from src.logging_config import get_logger
from src.notifications.models import NOTIFICATION_CHANNELS

logger = get_logger(__name__)

_IN_APP = "in_app"


# 方法作用：把数据库或内存时间统一转成 ISO 字符串，保证响应可 JSON 序列化。
# Args: value - datetime 或字符串时间。
# Returns: ISO 8601 字符串；空值返回 None。
def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    return str(value)


# 方法作用：把库里读到的 config_json 归一成字典，兼容驱动按文本返回 jsonb。
# Args: value - 数据库返回的原始值：本环境 asyncpg 实测返回 JSON 文本。
# Returns: 配置字典；无法解析时返回空字典。
def _as_config_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        # 本环境的 asyncpg 把 jsonb 按文本解码（'{}'::jsonb 也返回 str），
        # 直接 dict() 会逐字符迭代并抛 ValueError 把整条渠道读取打哑；
        # 若换成会解成对象的驱动，上面的 dict 分支照常可用。
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            logger.warning("渠道配置反序列化失败，按空配置处理", value_preview=value[:80])
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


# 方法作用：返回渠道配置的默认状态，站内信默认启用、外发渠道默认关闭。
# Args: channel - 渠道名。
# Returns: {"channel", "enabled", "config", "updated_by", "updated_at"} 结构。
def default_channel_state(channel: str) -> dict[str, Any]:
    return {
        "channel": channel,
        "enabled": channel == _IN_APP,
        "config": {},
        "updated_by": 0,
        "updated_at": None,
    }


class NotificationStore:
    """隔离通知主体、收件人行和租户渠道配置的持久化。"""

    # 方法作用：初始化可注入数据库连接池与内存分支字典。
    # Args: pg_pool - 可选 asyncpg Pool；dialect - postgres/sqlite；memory - 是否强制内存模式。
    # Returns: 无返回值。
    def __init__(
        self,
        pg_pool: Any | None = None,
        dialect: str = "postgres",
        memory: bool | None = None,
    ) -> None:
        self._pg_pool = pg_pool
        self.dialect = str(dialect or "postgres").lower()
        self._memory = bool(memory) if memory is not None else (pg_pool is None)
        self._notifications: dict[str, dict[str, Any]] = {}
        self._recipients: dict[tuple[str, int], dict[str, Any]] = {}
        self._channels: dict[tuple[int, str], dict[str, Any]] = {}
        self._tenants: dict[int, bool] = {}
        self._users: dict[int, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        logger.debug("NotificationStore 初始化完成", dialect=self.dialect, memory=self._memory)

    # ---- 内存分支的目录登记（测试与本地模式使用）----

    # 方法作用：在内存分支登记一个租户，供平台通知展开使用。
    # Args: self - Store；tenant_id - 租户 ID；is_active - 租户是否启用。
    # Returns: 无返回值。
    def register_tenant(self, tenant_id: int, is_active: bool = True) -> None:
        self._tenants[int(tenant_id)] = bool(is_active)
        logger.debug("通知 Store 登记租户", tenant_id=int(tenant_id), is_active=bool(is_active))

    # 方法作用：在内存分支登记一个用户，供 audience 展开使用。
    # Args: self - Store；tenant_id - 租户 ID；user_id - 用户 ID；role - 角色；is_active - 是否启用。
    # Returns: 无返回值。
    def register_user(
        self,
        tenant_id: int,
        user_id: int,
        role: str = "analyst",
        is_active: bool = True,
    ) -> None:
        self._tenants.setdefault(int(tenant_id), True)
        self._users[int(user_id)] = {
            "tenant_id": int(tenant_id),
            "role": str(role).strip().lower(),
            "is_active": bool(is_active),
        }
        logger.debug(
            "通知 Store 登记用户",
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            role=str(role),
        )

    # ---- 投递目标目录 ----

    # 方法作用：列出全部启用租户，供平台通知按租户各写一条。
    # Args: self - Store。
    # Returns: 租户 ID 升序列表。
    async def list_tenants(self) -> list[int]:
        logger.debug("通知租户列表查询入口", memory=self._memory)
        if self._memory:
            result = sorted(tid for tid, active in self._tenants.items() if active)
            logger.info("通知租户列表查询完成", count=len(result), memory=True)
            return result
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            rows = await connection.fetch(
                "SELECT id FROM tenants WHERE COALESCE(is_active, TRUE) ORDER BY id"
            )
        result = [int(row["id"]) for row in rows]
        logger.info("通知租户列表查询完成", count=len(result))
        return result

    # 方法作用：列出指定租户的启用用户，可按角色过滤。
    # Args: self - Store；tenant_id - 租户 ID；roles - 可选角色过滤列表。
    # Returns: [{"id", "role"}] 升序列表。
    async def list_users(self, tenant_id: int, roles: list[str] | None = None) -> list[dict[str, Any]]:
        normalized_roles = [str(role).strip().lower() for role in roles or []]
        logger.debug(
            "通知用户列表查询入口",
            tenant_id=int(tenant_id),
            roles=normalized_roles,
            memory=self._memory,
        )
        if self._memory:
            result = [
                {"id": user_id, "role": user["role"]}
                for user_id, user in sorted(self._users.items())
                if user["tenant_id"] == int(tenant_id)
                and user["is_active"]
                and (not normalized_roles or user["role"] in normalized_roles)
            ]
            logger.info("通知用户列表查询完成", tenant_id=int(tenant_id), count=len(result), memory=True)
            return result
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            if normalized_roles:
                rows = await connection.fetch(
                    "SELECT id, LOWER(role) AS role FROM users "
                    "WHERE tenant_id=$1 AND COALESCE(is_active, TRUE) "
                    "AND LOWER(role) = ANY($2::text[]) ORDER BY id",
                    int(tenant_id),
                    normalized_roles,
                )
            else:
                rows = await connection.fetch(
                    "SELECT id, LOWER(role) AS role FROM users "
                    "WHERE tenant_id=$1 AND COALESCE(is_active, TRUE) ORDER BY id",
                    int(tenant_id),
                )
        result = [{"id": int(row["id"]), "role": str(row["role"])} for row in rows]
        logger.info("通知用户列表查询完成", tenant_id=int(tenant_id), count=len(result))
        return result

    # 方法作用：从候选 ID 中筛出确实属于该租户的启用用户。
    # Args: self - Store；tenant_id - 租户 ID；user_ids - 候选用户 ID 列表。
    # Returns: 命中租户的启用用户 ID 列表（保持输入顺序）。
    async def filter_tenant_users(self, tenant_id: int, user_ids: list[int]) -> list[int]:
        candidates = list(dict.fromkeys(int(user_id) for user_id in user_ids))
        logger.debug("通知租户用户筛选入口", tenant_id=int(tenant_id), candidate_count=len(candidates))
        if not candidates:
            return []
        if self._memory:
            result = [
                user_id
                for user_id in candidates
                if (user := self._users.get(user_id)) is not None
                and user["tenant_id"] == int(tenant_id)
                and user["is_active"]
            ]
        else:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
                rows = await connection.fetch(
                    "SELECT id FROM users WHERE tenant_id=$1 AND COALESCE(is_active, TRUE) "
                    "AND id = ANY($2::bigint[])",
                    int(tenant_id),
                    candidates,
                )
            matched = {int(row["id"]) for row in rows}
            result = [user_id for user_id in candidates if user_id in matched]
        logger.info(
            "通知租户用户筛选完成",
            tenant_id=int(tenant_id),
            matched=len(result),
            candidate_count=len(candidates),
        )
        return result

    # ---- 通知写入 ----

    # 方法作用：写入一条通知主体和它的全部收件人行，收件人的 created_at 冗余主体时间。
    # Args: self - Store；tenant_id - 租户 ID；kind - 通知类型；title - 标题；body - 正文；
    #       link - 跳转链接；created_by - 发布人；audience - 投递范围字典；recipients - 收件人 ID 列表。
    # Returns: {"id", "created_at"} 写入结果。
    async def insert_notification(
        self,
        *,
        tenant_id: int,
        kind: str,
        title: str,
        body: str,
        link: str,
        created_by: int,
        audience: dict[str, Any],
        recipients: list[int],
    ) -> dict[str, Any]:
        notification_id = str(uuid4())
        created_at = datetime.now(timezone.utc)
        unique_recipients = list(dict.fromkeys(int(user_id) for user_id in recipients))
        logger.debug(
            "通知写入入口",
            tenant_id=int(tenant_id),
            kind=kind,
            recipient_count=len(unique_recipients),
            memory=self._memory,
        )
        if self._memory:
            self._notifications[notification_id] = {
                "id": notification_id,
                "tenant_id": int(tenant_id),
                "kind": kind,
                "title": title,
                "body": body,
                "link": link,
                "created_by": int(created_by),
                "audience": dict(audience or {}),
                "created_at": created_at,
            }
            for user_id in unique_recipients:
                self._recipients[(notification_id, user_id)] = {
                    "notification_id": notification_id,
                    "tenant_id": int(tenant_id),
                    "user_id": user_id,
                    "is_read": False,
                    "read_at": None,
                    "deleted_at": None,
                    "created_at": created_at,
                }
        else:
            import json

            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO notifications "
                        "(id,tenant_id,kind,title,body,link,created_by,audience_json,created_at) "
                        "VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)",
                        notification_id,
                        int(tenant_id),
                        kind,
                        title,
                        body,
                        link,
                        int(created_by),
                        json.dumps(dict(audience or {}), ensure_ascii=False),
                        created_at,
                    )
                    if unique_recipients:
                        await connection.execute(
                            "INSERT INTO notification_recipients "
                            "(notification_id,tenant_id,user_id,created_at) "
                            "SELECT $1::uuid, $2, unnest($3::bigint[]), $4",
                            notification_id,
                            int(tenant_id),
                            unique_recipients,
                            created_at,
                        )
        logger.info(
            "通知写入完成",
            notification_id=notification_id,
            tenant_id=int(tenant_id),
            kind=kind,
            recipient_count=len(unique_recipients),
        )
        return {"id": notification_id, "created_at": _iso(created_at)}

    # ---- 收件箱读取 ----

    # 方法作用：分页读取当前身份收件箱，排除软删行并按收件时间倒序。
    # Args: self - Store；identity - 当前身份；unread_only - 只看未读；page - 页码；page_size - 每页条数。
    # Returns: {"items", "total", "page", "page_size"}。
    async def list_inbox(
        self,
        identity: Any,
        *,
        unread_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        tenant_id = int(identity.tenant_id)
        user_id = int(identity.user_id)
        offset = (page - 1) * page_size
        logger.debug(
            "收件箱查询入口",
            tenant_id=tenant_id,
            user_id=user_id,
            unread_only=bool(unread_only),
            page=page,
            page_size=page_size,
            memory=self._memory,
        )
        if self._memory:
            rows = [
                (recipient, self._notifications.get(recipient["notification_id"], {}))
                for recipient in self._recipients.values()
                if recipient["tenant_id"] == tenant_id
                and recipient["user_id"] == user_id
                and recipient["deleted_at"] is None
                and (not unread_only or not recipient["is_read"])
            ]
            # created_at 相同时用通知 ID 兜底，保证分页顺序稳定。
            rows.sort(key=lambda item: (item[0]["created_at"], item[0]["notification_id"]), reverse=True)
            total = len(rows)
            selected = rows[offset:offset + page_size]
            items = [
                {
                    "id": recipient["notification_id"],
                    "kind": notification.get("kind", ""),
                    "title": notification.get("title", ""),
                    "body": notification.get("body", ""),
                    "link": notification.get("link", ""),
                    "created_at": _iso(recipient["created_at"]),
                    "is_read": bool(recipient["is_read"]),
                    "read_at": _iso(recipient["read_at"]),
                }
                for recipient, notification in selected
            ]
        else:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            unread_clause = " AND is_read=FALSE" if unread_only else ""
            async with pg_pool_connection(pool, tenant_id, user_id, str(identity.role)) as connection:
                total = int(
                    await connection.fetchval(
                        "SELECT COUNT(*) FROM notification_recipients "
                        "WHERE tenant_id=$1 AND user_id=$2 AND deleted_at IS NULL" + unread_clause,
                        tenant_id,
                        user_id,
                    )
                    or 0
                )
                rows = await connection.fetch(
                    "SELECT n.id::text AS id, n.kind, n.title, n.body, n.link, "
                    "r.created_at, r.is_read, r.read_at "
                    "FROM notification_recipients r "
                    "JOIN notifications n ON n.id = r.notification_id "
                    "WHERE r.tenant_id=$1 AND r.user_id=$2 AND r.deleted_at IS NULL"
                    + unread_clause
                    + " ORDER BY r.created_at DESC, n.id DESC LIMIT $3 OFFSET $4",
                    tenant_id,
                    user_id,
                    page_size,
                    offset,
                )
            items = [
                {
                    "id": str(row["id"]),
                    "kind": str(row["kind"]),
                    "title": str(row["title"]),
                    "body": str(row["body"]),
                    "link": str(row["link"]),
                    "created_at": _iso(row["created_at"]),
                    "is_read": bool(row["is_read"]),
                    "read_at": _iso(row["read_at"]),
                }
                for row in rows
            ]
        logger.info(
            "收件箱查询完成",
            tenant_id=tenant_id,
            user_id=user_id,
            total=total,
            returned=len(items),
        )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    # 方法作用：统计当前身份的未读数，用于未读角标。
    # Args: self - Store；identity - 当前身份。
    # Returns: 未读条数。
    async def unread_count(self, identity: Any) -> int:
        tenant_id = int(identity.tenant_id)
        user_id = int(identity.user_id)
        logger.debug("未读数查询入口", tenant_id=tenant_id, user_id=user_id, memory=self._memory)
        if self._memory:
            count = sum(
                1
                for recipient in self._recipients.values()
                if recipient["tenant_id"] == tenant_id
                and recipient["user_id"] == user_id
                and recipient["deleted_at"] is None
                and not recipient["is_read"]
            )
        else:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, tenant_id, user_id, str(identity.role)) as connection:
                count = int(
                    await connection.fetchval(
                        "SELECT COUNT(*) FROM notification_recipients "
                        "WHERE tenant_id=$1 AND user_id=$2 AND deleted_at IS NULL AND is_read=FALSE",
                        tenant_id,
                        user_id,
                    )
                    or 0
                )
        logger.info("未读数查询完成", tenant_id=tenant_id, user_id=user_id, count=count)
        return count

    # ---- 已读与删除 ----

    # 方法作用：把当前身份的一条通知标记为已读。
    # Args: self - Store；identity - 当前身份；notification_id - 通知 ID。
    # Returns: 命中收件人行返回 True，否则 False。
    async def mark_read(self, identity: Any, notification_id: str) -> bool:
        tenant_id = int(identity.tenant_id)
        user_id = int(identity.user_id)
        logger.debug("标记已读入口", tenant_id=tenant_id, user_id=user_id, notification_id=notification_id, memory=self._memory)
        if self._memory:
            recipient = self._recipients.get((notification_id, user_id))
            if recipient is None or recipient["tenant_id"] != tenant_id or recipient["deleted_at"] is not None:
                logger.info("标记已读未命中", notification_id=notification_id, user_id=user_id)
                return False
            recipient["is_read"] = True
            recipient["read_at"] = datetime.now(timezone.utc)
            logger.info("标记已读完成", notification_id=notification_id, user_id=user_id)
            return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, tenant_id, user_id, str(identity.role)) as connection:
            updated = await connection.fetchval(
                "UPDATE notification_recipients SET is_read=TRUE, read_at=NOW() "
                "WHERE notification_id=$1::uuid AND tenant_id=$2 AND user_id=$3 "
                "AND deleted_at IS NULL RETURNING notification_id",
                notification_id,
                tenant_id,
                user_id,
            )
        matched = updated is not None
        logger.info("标记已读完成", notification_id=notification_id, user_id=user_id, matched=matched)
        return matched

    # 方法作用：把当前身份的全部未读通知标记为已读。
    # Args: self - Store；identity - 当前身份。
    # Returns: 实际更新条数。
    async def mark_all_read(self, identity: Any) -> int:
        tenant_id = int(identity.tenant_id)
        user_id = int(identity.user_id)
        logger.debug("全部已读入口", tenant_id=tenant_id, user_id=user_id, memory=self._memory)
        if self._memory:
            updated = 0
            now = datetime.now(timezone.utc)
            for recipient in self._recipients.values():
                if (
                    recipient["tenant_id"] == tenant_id
                    and recipient["user_id"] == user_id
                    and recipient["deleted_at"] is None
                    and not recipient["is_read"]
                ):
                    recipient["is_read"] = True
                    recipient["read_at"] = now
                    updated += 1
        else:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, tenant_id, user_id, str(identity.role)) as connection:
                rows = await connection.fetch(
                    "UPDATE notification_recipients SET is_read=TRUE, read_at=NOW() "
                    "WHERE tenant_id=$1 AND user_id=$2 AND deleted_at IS NULL AND is_read=FALSE "
                    "RETURNING notification_id",
                    tenant_id,
                    user_id,
                )
            updated = len(rows)
        logger.info("全部已读完成", tenant_id=tenant_id, user_id=user_id, updated=updated)
        return updated

    # 方法作用：软删当前身份的一条通知，仅写 deleted_at，保留审计痕迹。
    # Args: self - Store；identity - 当前身份；notification_id - 通知 ID。
    # Returns: 命中收件人行返回 True，否则 False。
    async def soft_delete(self, identity: Any, notification_id: str) -> bool:
        tenant_id = int(identity.tenant_id)
        user_id = int(identity.user_id)
        logger.debug("通知软删入口", tenant_id=tenant_id, user_id=user_id, notification_id=notification_id, memory=self._memory)
        if self._memory:
            recipient = self._recipients.get((notification_id, user_id))
            if recipient is None or recipient["tenant_id"] != tenant_id or recipient["deleted_at"] is not None:
                logger.info("通知软删未命中", notification_id=notification_id, user_id=user_id)
                return False
            recipient["deleted_at"] = datetime.now(timezone.utc)
            logger.info("通知软删完成", notification_id=notification_id, user_id=user_id)
            return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, tenant_id, user_id, str(identity.role)) as connection:
            updated = await connection.fetchval(
                "UPDATE notification_recipients SET deleted_at=NOW() "
                "WHERE notification_id=$1::uuid AND tenant_id=$2 AND user_id=$3 "
                "AND deleted_at IS NULL RETURNING notification_id",
                notification_id,
                tenant_id,
                user_id,
            )
        matched = updated is not None
        logger.info("通知软删完成", notification_id=notification_id, user_id=user_id, matched=matched)
        return matched

    # ---- 租户渠道配置 ----

    # 方法作用：读取租户渠道配置，缺失渠道用默认状态兜底。
    # Args: self - Store；tenant_id - 租户 ID。
    # Returns: 四个渠道的原始配置行列表（敏感值仍是密文，由服务层脱敏）。
    async def get_channels(self, tenant_id: int) -> list[dict[str, Any]]:
        tenant_id = int(tenant_id)
        logger.debug("渠道配置查询入口", tenant_id=tenant_id, memory=self._memory)
        if self._memory:
            stored = {
                channel: dict(self._channels[(tenant_id, channel)])
                for channel in NOTIFICATION_CHANNELS
                if (tenant_id, channel) in self._channels
            }
        else:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, tenant_id, 0, "tenant_admin") as connection:
                rows = await connection.fetch(
                    "SELECT channel, enabled, config_json, updated_by, updated_at "
                    "FROM notification_channels WHERE tenant_id=$1",
                    tenant_id,
                )
            stored = {
                str(row["channel"]): {
                    "channel": str(row["channel"]),
                    "enabled": bool(row["enabled"]),
                    "config": _as_config_dict(row["config_json"]),
                    "updated_by": int(row["updated_by"] or 0),
                    "updated_at": row["updated_at"],
                }
                for row in rows
            }
        result: list[dict[str, Any]] = []
        for channel in NOTIFICATION_CHANNELS:
            default = default_channel_state(channel)
            row = stored.get(channel)
            if row is None:
                result.append(default)
                continue
            default.update({"enabled": bool(row.get("enabled", False)), "config": dict(row.get("config") or {})})
            default["updated_by"] = int(row.get("updated_by") or 0)
            default["updated_at"] = _iso(row.get("updated_at"))
            result.append(default)
        logger.info("渠道配置查询完成", tenant_id=tenant_id, channel_count=len(result))
        return result

    # 方法作用：保存租户渠道配置，未知渠道保持原状不新建。
    # Args: self - Store；tenant_id - 租户 ID；channels - 渠道名到 {enabled, config} 的字典；
    #       updated_by - 操作人。
    # Returns: 保存后的完整渠道配置行列表。
    async def save_channels(
        self,
        tenant_id: int,
        channels: dict[str, dict[str, Any]],
        updated_by: int,
    ) -> list[dict[str, Any]]:
        tenant_id = int(tenant_id)
        targets = {
            channel: setting
            for channel, setting in channels.items()
            if channel in NOTIFICATION_CHANNELS
        }
        logger.debug(
            "渠道配置保存入口",
            tenant_id=tenant_id,
            channels=sorted(targets),
            updated_by=int(updated_by),
            memory=self._memory,
        )
        if self._memory:
            now = datetime.now(timezone.utc)
            for channel, setting in targets.items():
                self._channels[(tenant_id, channel)] = {
                    "channel": channel,
                    "enabled": bool(setting.get("enabled", False)),
                    "config": dict(setting.get("config") or {}),
                    "updated_by": int(updated_by),
                    "updated_at": now,
                }
        elif targets:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, tenant_id, 0, "tenant_admin") as connection:
                async with connection.transaction():
                    for channel, setting in targets.items():
                        await connection.execute(
                            "INSERT INTO notification_channels "
                            "(tenant_id,channel,enabled,config_json,updated_by,updated_at) "
                            "VALUES ($1,$2,$3,$4::jsonb,$5,NOW()) "
                            "ON CONFLICT (tenant_id, channel) DO UPDATE SET "
                            "enabled=EXCLUDED.enabled, config_json=EXCLUDED.config_json, "
                            "updated_by=EXCLUDED.updated_by, updated_at=NOW()",
                            tenant_id,
                            channel,
                            bool(setting.get("enabled", False)),
                            # 本环境的 asyncpg 对 jsonb 参数只接受字符串（传 dict 会报
                            # "expected str, got dict"），因此写入侧必须自己序列化；
                            # 服务端仍会存成真正的 jsonb 对象，不是双重编码。
                            json.dumps(dict(setting.get("config") or {}), ensure_ascii=False),
                            int(updated_by),
                        )
        result = await self.get_channels(tenant_id)
        logger.info("渠道配置保存完成", tenant_id=tenant_id, channels=sorted(targets))
        return result

    # 方法作用：惰性获取共享 PostgreSQL Pool。
    # Args: self - Store。
    # Returns: asyncpg Pool。
    async def _pool(self):
        if self._pg_pool is None:
            from src.memory.pg_pool import get_pg_pool

            self._pg_pool = await get_pg_pool()
        return self._pg_pool


_store: NotificationStore | None = None


# 方法作用：获取进程级通知 Store；未配置数据库时退回内存分支。
# Args: 无。
# Returns: 当前进程共享的 NotificationStore。
def get_notification_store() -> NotificationStore:
    global _store
    if _store is None:
        settings = get_settings()
        database_url = str(getattr(settings, "database_url", "") or "").strip()
        _store = NotificationStore(memory=not database_url)
    return _store


# 方法作用：用注入的 Store 替换进程级实例，供测试与运行时装配使用。
# Args: store - 新的 Store 实例；None 表示恢复惰性创建。
# Returns: 无返回值。
def set_notification_store(store: NotificationStore | None) -> None:
    global _store
    _store = store
    logger.info("通知 Store 已替换", injected=store is not None)
