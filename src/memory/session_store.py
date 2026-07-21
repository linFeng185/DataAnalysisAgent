"""会话元数据 PG 持久化 + 内存回退。

PG 可用时双写（内存 + PG），不可用时仅内存。
内存模式确保功能立即可用，重启后 PG 中有数据的可恢复。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection

logger = get_logger(__name__)

_MAX_MEMORY = 500


class SessionStore:
    """会话元数据 CRUD。PG 优先，内存回退。"""

    def __init__(self) -> None:
        self._items: list[dict] = []  # 内存缓冲
        self._pg_ready: bool | None = None

    async def _ensure_pg(self) -> bool:
        """确保包含身份列的 PG 表存在。

        Returns:
            PostgreSQL 可用且表结构就绪时返回 True。
        """
        logger.debug("会话 PG 初始化入口", cached=self._pg_ready)
        if self._pg_ready is not None:
            logger.info("会话 PG 初始化命中缓存", ready=self._pg_ready)
            return self._pg_ready
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            self._pg_ready = False
            return False
        try:
            async with pg_connection(tenant_id=1, user_id=0, role="super_admin") as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        title TEXT DEFAULT '',
                        datasource TEXT DEFAULT '',
                        first_query TEXT DEFAULT '',
                        user_id INT NOT NULL DEFAULT 0,
                        tenant_id INT NOT NULL DEFAULT 1,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        last_active_at TIMESTAMPTZ DEFAULT NOW(),
                        turn_count INT DEFAULT 0
                    )
                """)
                await conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id INT NOT NULL DEFAULT 0")
                await conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id INT NOT NULL DEFAULT 1")
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_identity_active "
                    "ON sessions (tenant_id, user_id, last_active_at DESC)")
            self._pg_ready = True
            logger.info("sessions 表已就绪（PG）")
            return True
        except Exception as exc:
            logger.error("sessions PG 不可用，使用内存模式", error=str(exc), exc_info=True)
            self._pg_ready = False
            return False

    @asynccontextmanager
    async def _pg_conn(self) -> AsyncIterator[Any]:
        """创建已注入当前身份参数的 PG 连接。

        Returns:
            可用的 asyncpg 连接；连接失败返回 None。
        """
        from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("会话 PG 连接入口", user_id=user_id, tenant_id=tenant_id)
        try:
            async with pg_connection(
                tenant_id=tenant_id,
                user_id=user_id,
                role=get_current_role(),
            ) as conn:
                yield conn
            logger.info("会话 PG 连接完成", user_id=user_id, tenant_id=tenant_id)
        except Exception as exc:
            logger.error("会话 PG 连接失败", error=str(exc), exc_info=True)
            raise

    def _mem_item(self, session_id: str, title: str, datasource: str, first_query: str,
                  turn_count: int = 1) -> dict:
        """创建绑定当前身份的内存会话记录。

        Args:
            session_id: 对外会话 ID。
            title: 会话标题。
            datasource: 数据源名称。
            first_query: 首次用户查询。
            turn_count: 初始轮次数。

        Returns:
            可写入内存缓冲区的会话字典。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        logger.debug("构建内存会话入口", session_id=session_id[:20])
        now = datetime.now(timezone.utc)
        item = {
            "session_id": session_id, "title": title, "datasource": datasource,
            "first_query": first_query, "turn_count": turn_count,
            "user_id": get_current_user_id(), "tenant_id": get_current_tenant_id(),
            "created_at": now.isoformat(), "last_active_at": now.isoformat(),
        }
        logger.info("构建内存会话完成", session_id=session_id[:20], user_id=item["user_id"])
        return item

    async def create(self, session_id: str, datasource: str, first_query: str) -> bool:
        """为当前身份创建新会话，title 由 first_query 截断。

        Args:
            session_id: 对外会话 ID。
            datasource: 数据源名称。
            first_query: 首次用户查询。

        Returns:
            创建成功返回 True。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("创建会话入口", session_id=session_id[:20], user_id=user_id, tenant_id=tenant_id)
        title = (first_query[:30] + "…") if len(first_query) > 30 else first_query

        # 内存写入（始终成功）
        self._items.append(self._mem_item(session_id, title, datasource, first_query))
        if len(self._items) > _MAX_MEMORY:
            self._items = self._items[-_MAX_MEMORY:]

        # PG 写入
        if await self._ensure_pg():
            try:
                async with self._pg_conn() as conn:
                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        "INSERT INTO sessions "
                        "(session_id, title, datasource, first_query, user_id, tenant_id, "
                        "created_at, last_active_at, turn_count) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $7, 1) "
                        "ON CONFLICT (session_id) DO UPDATE SET "
                        "last_active_at = $7, turn_count = sessions.turn_count + 1 "
                        "WHERE sessions.user_id = EXCLUDED.user_id "
                        "AND sessions.tenant_id = EXCLUDED.tenant_id",
                        session_id, title, datasource, first_query, user_id, tenant_id, now)
            except Exception as exc:
                logger.error("会话 PG 写入失败", error=str(exc), exc_info=True)

        logger.info("会话已创建", session_id=session_id[:20], user_id=user_id, tenant_id=tenant_id)
        return True

    async def touch(self, session_id: str, datasource: str = "", first_query: str = "") -> bool:
        """更新会话活跃时间，不存在时自动创建（UPSERT）。

        前端首次请求就带 session_id，后端无法区分新旧——
        用 UPSERT 兜底：记录已存在则 UPDATE，不存在则 INSERT。

        Args:
            session_id - 会话 ID
            datasource - 数据源（创建时填入）
            first_query - 首次提问（创建时填入 title）
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("更新会话入口", session_id=session_id[:20], user_id=user_id, tenant_id=tenant_id)
        now = datetime.now(timezone.utc)

        # 内存更新/创建
        found = False
        for item in self._items:
            if (item["session_id"] == session_id
                    and item.get("user_id") == user_id
                    and item.get("tenant_id") == tenant_id):
                item["last_active_at"] = now.isoformat()
                item["turn_count"] = item.get("turn_count", 0) + 1
                found = True
                break
        if not found:
            title = (first_query[:30] + "…") if len(first_query) > 30 else first_query
            self._items.append(self._mem_item(session_id, title, datasource, first_query))

        # PG UPSERT
        if await self._ensure_pg():
            try:
                async with self._pg_conn() as conn:
                    status = await conn.execute(
                        "UPDATE sessions SET last_active_at = $1, turn_count = turn_count + 1 "
                        "WHERE session_id = $2 AND user_id = $3 AND tenant_id = $4",
                        now, session_id, user_id, tenant_id,
                    )
                    if status.endswith(" 0"):
                        title = (first_query[:30] + "…") if len(first_query) > 30 else first_query
                        await conn.execute(
                            "INSERT INTO sessions "
                            "(session_id, title, datasource, first_query, user_id, tenant_id, "
                            "created_at, last_active_at, turn_count) "
                            "VALUES ($1, $2, $3, $4, $5, $6, $7, $7, 1) "
                            "ON CONFLICT (session_id) DO NOTHING",
                            session_id, title, datasource, first_query, user_id, tenant_id, now)
            except Exception as exc:
                logger.error("会话 PG 更新失败", error=str(exc), exc_info=True)
        logger.info("更新会话完成", session_id=session_id[:20], user_id=user_id)
        return True

    async def list(self, cursor: str | None = None, limit: int = 20) -> list[dict]:
        """按当前身份游标分页列出会话。

        Args:
            cursor: 上一页最后活跃时间。
            limit: 最大返回条数。

        Returns:
            当前身份可见的会话列表。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("列出会话入口", user_id=user_id, tenant_id=tenant_id, cursor=cursor, limit=limit)
        if await self._ensure_pg():
            try:
                async with self._pg_conn() as conn:
                    if cursor:
                        pg_cursor = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
                        if pg_cursor.tzinfo is None:
                            logger.warning("会话游标缺少时区，按 UTC 解析", cursor=cursor)
                            pg_cursor = pg_cursor.replace(tzinfo=timezone.utc)
                        logger.info("会话 PG 游标解析完成", cursor=pg_cursor.isoformat())
                        rows = await conn.fetch(
                            "SELECT session_id, title, datasource, turn_count, created_at, last_active_at "
                            "FROM sessions WHERE tenant_id = $1 AND user_id = $2 "
                            "AND last_active_at < $3::timestamptz "
                            "ORDER BY last_active_at DESC LIMIT $4", tenant_id, user_id, pg_cursor, limit)
                    else:
                        rows = await conn.fetch(
                            "SELECT session_id, title, datasource, turn_count, created_at, last_active_at "
                            "FROM sessions WHERE tenant_id = $1 AND user_id = $2 "
                            "ORDER BY last_active_at DESC LIMIT $3", tenant_id, user_id, limit)
                    result = [_row_to_dict(r) for r in rows]
                    logger.info("列出会话完成", source="postgres", count=len(result), user_id=user_id)
                    return result
            except Exception as exc:
                logger.error("会话 PG 查询失败", error=str(exc), exc_info=True)

        # 内存回退：按 last_active_at 降序，游标分页
        visible_items = [
            item for item in self._items
            if item.get("user_id") == user_id and item.get("tenant_id") == tenant_id
        ]
        sorted_items = sorted(visible_items, key=lambda x: x.get("last_active_at", ""), reverse=True)
        if cursor:
            sorted_items = [i for i in sorted_items if i.get("last_active_at", "") < cursor]
        result = sorted_items[:limit]
        logger.info("列出会话完成", source="memory", count=len(result), user_id=user_id)
        return result

    async def get(self, session_id: str) -> dict | None:
        """获取当前身份拥有的单个会话元数据。

        Args:
            session_id: 对外会话 ID。

        Returns:
            可见的会话字典；不存在或无权访问返回 None。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("获取会话入口", session_id=session_id[:20], user_id=user_id, tenant_id=tenant_id)
        if await self._ensure_pg():
            try:
                async with self._pg_conn() as conn:
                    row = await conn.fetchrow(
                        "SELECT session_id, title, datasource, turn_count, created_at, last_active_at "
                        "FROM sessions WHERE session_id = $1 AND tenant_id = $2 AND user_id = $3",
                        session_id, tenant_id, user_id)
                    if row:
                        result = _row_to_dict(row)
                        logger.info("获取会话完成", source="postgres", found=True, user_id=user_id)
                        return result
            except Exception as exc:
                logger.error("会话 PG 读取失败", error=str(exc), exc_info=True)

        for item in self._items:
            if (item["session_id"] == session_id
                    and item.get("tenant_id") == tenant_id
                    and item.get("user_id") == user_id):
                logger.info("获取会话完成", source="memory", found=True, user_id=user_id)
                return item
        logger.info("获取会话完成", source="memory", found=False, user_id=user_id)
        return None

    async def delete(self, session_id: str) -> bool:
        """删除当前身份拥有的会话。

        Args:
            session_id: 对外会话 ID。

        Returns:
            确实删除了可见会话时返回 True。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("删除会话入口", session_id=session_id[:20], user_id=user_id, tenant_id=tenant_id)
        original_count = len(self._items)
        self._items = [
            item for item in self._items
            if not (
                item["session_id"] == session_id
                and item.get("tenant_id") == tenant_id
                and item.get("user_id") == user_id
            )
        ]
        deleted = len(self._items) < original_count

        if await self._ensure_pg():
            try:
                async with self._pg_conn() as conn:
                    status = await conn.execute(
                        "DELETE FROM sessions WHERE session_id = $1 AND tenant_id = $2 AND user_id = $3",
                        session_id, tenant_id, user_id,
                    )
                    deleted = deleted or not status.endswith(" 0")
            except Exception as exc:
                logger.error("会话 PG 删除失败", error=str(exc), exc_info=True)
        logger.info("删除会话完成", session_id=session_id[:20], deleted=deleted, user_id=user_id)
        return deleted


def _row_to_dict(row) -> dict:
    return {
        "session_id": row["session_id"],
        "title": row["title"] or "",
        "datasource": row["datasource"] or "",
        "turn_count": row["turn_count"] or 0,
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "last_active_at": row["last_active_at"].isoformat() if row["last_active_at"] else "",
    }


_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
