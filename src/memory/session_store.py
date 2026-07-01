"""会话元数据 PG 持久化 + 内存回退。

PG 可用时双写（内存 + PG），不可用时仅内存。
内存模式确保功能立即可用，重启后 PG 中有数据的可恢复。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)

_MAX_MEMORY = 500


class SessionStore:
    """会话元数据 CRUD。PG 优先，内存回退。"""

    def __init__(self) -> None:
        self._items: list[dict] = []  # 内存缓冲
        self._pg_ready: bool | None = None

    async def _ensure_pg(self) -> bool:
        """确保 PG 表存在。"""
        if self._pg_ready is not None:
            return self._pg_ready
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            self._pg_ready = False
            return False
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    datasource TEXT DEFAULT '',
                    first_query TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_active_at TIMESTAMPTZ DEFAULT NOW(),
                    turn_count INT DEFAULT 0
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions (last_active_at DESC)")
            await conn.close()
            self._pg_ready = True
            logger.info("sessions 表已就绪（PG）")
            return True
        except Exception as e:
            logger.warning("sessions PG 不可用，使用内存模式", error=str(e))
            self._pg_ready = False
            return False

    async def _pg_conn(self):
        s = get_settings()
        url = s.database_url
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            return await asyncpg.connect(pg_url)
        except Exception:
            return None

    def _mem_item(self, session_id: str, title: str, datasource: str, first_query: str,
                  turn_count: int = 1) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "session_id": session_id, "title": title, "datasource": datasource,
            "first_query": first_query, "turn_count": turn_count,
            "created_at": now.isoformat(), "last_active_at": now.isoformat(),
        }

    async def create(self, session_id: str, datasource: str, first_query: str) -> bool:
        """创建新会话，title 由 first_query 截断。"""
        title = (first_query[:30] + "…") if len(first_query) > 30 else first_query

        # 内存写入（始终成功）
        self._items.append(self._mem_item(session_id, title, datasource, first_query))
        if len(self._items) > _MAX_MEMORY:
            self._items = self._items[-_MAX_MEMORY:]

        # PG 写入
        if await self._ensure_pg():
            conn = await self._pg_conn()
            if conn:
                try:
                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        "INSERT INTO sessions (session_id, title, datasource, first_query, created_at, last_active_at, turn_count) "
                        "VALUES ($1, $2, $3, $4, $5, $5, 1) ON CONFLICT (session_id) DO UPDATE SET "
                        "last_active_at = $5, turn_count = sessions.turn_count + 1",
                        session_id, title, datasource, first_query, now)
                except Exception as e:
                    logger.debug("会话 PG 写入失败", error=str(e))
                finally:
                    await conn.close()

        logger.info("会话已创建", session_id=session_id[:20])
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
        now = datetime.now(timezone.utc)

        # 内存更新/创建
        found = False
        for item in self._items:
            if item["session_id"] == session_id:
                item["last_active_at"] = now.isoformat()
                item["turn_count"] = item.get("turn_count", 0) + 1
                found = True
                break
        if not found:
            title = (first_query[:30] + "…") if len(first_query) > 30 else first_query
            self._items.append(self._mem_item(session_id, title, datasource, first_query))

        # PG UPSERT
        if await self._ensure_pg():
            conn = await self._pg_conn()
            if conn:
                try:
                    if found or not first_query:
                        await conn.execute(
                            "UPDATE sessions SET last_active_at = $1, turn_count = turn_count + 1 "
                            "WHERE session_id = $2", now, session_id)
                    else:
                        title = (first_query[:30] + "…") if len(first_query) > 30 else first_query
                        await conn.execute(
                            "INSERT INTO sessions (session_id, title, datasource, first_query, last_active_at, turn_count) "
                            "VALUES ($1, $2, $3, $4, $5, 1) ON CONFLICT (session_id) DO UPDATE SET "
                            "last_active_at = $5, turn_count = sessions.turn_count + 1",
                            session_id, title, datasource, first_query, now)
                except Exception:
                    pass
                finally:
                    await conn.close()
        return True

    async def list(self, cursor: str | None = None, limit: int = 20) -> list[dict]:
        """游标分页列出会话。PG 优先，内存回退。"""
        if await self._ensure_pg():
            conn = await self._pg_conn()
            if conn:
                try:
                    if cursor:
                        rows = await conn.fetch(
                            "SELECT session_id, title, datasource, turn_count, created_at, last_active_at "
                            "FROM sessions WHERE last_active_at < $1::timestamptz "
                            "ORDER BY last_active_at DESC LIMIT $2", cursor, limit)
                    else:
                        rows = await conn.fetch(
                            "SELECT session_id, title, datasource, turn_count, created_at, last_active_at "
                            "FROM sessions ORDER BY last_active_at DESC LIMIT $1", limit)
                    await conn.close()
                    return [_row_to_dict(r) for r in rows]
                except Exception:
                    pass
                finally:
                    try:
                        await conn.close()
                    except Exception:
                        pass

        # 内存回退：按 last_active_at 降序，游标分页
        sorted_items = sorted(self._items, key=lambda x: x.get("last_active_at", ""), reverse=True)
        if cursor:
            sorted_items = [i for i in sorted_items if i.get("last_active_at", "") < cursor]
        return sorted_items[:limit]

    async def get(self, session_id: str) -> dict | None:
        """获取单个会话元数据。"""
        if await self._ensure_pg():
            conn = await self._pg_conn()
            if conn:
                try:
                    row = await conn.fetchrow(
                        "SELECT session_id, title, datasource, turn_count, created_at, last_active_at "
                        "FROM sessions WHERE session_id = $1", session_id)
                    await conn.close()
                    if row:
                        return _row_to_dict(row)
                except Exception:
                    pass
                finally:
                    try:
                        await conn.close()
                    except Exception:
                        pass

        for item in self._items:
            if item["session_id"] == session_id:
                return item
        return None

    async def delete(self, session_id: str) -> bool:
        """删除会话。"""
        self._items = [i for i in self._items if i["session_id"] != session_id]

        if await self._ensure_pg():
            conn = await self._pg_conn()
            if conn:
                try:
                    await conn.execute("DELETE FROM sessions WHERE session_id = $1", session_id)
                except Exception:
                    pass
                finally:
                    await conn.close()
        return True


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
