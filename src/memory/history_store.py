"""查询历史存储 — 内存环形缓冲区 + PG 持久化。

PG 可用时优先读写，不可用时回退内存模式。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)

_MAX_SIZE = 500


class HistoryStore:
    """查询历史存储，PG 优先，内存回退。"""

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._pg_ready: bool | None = None  # None=未检测

    async def _ensure_pg(self) -> bool:
        """确保 PG 表存在，返回是否可用。"""
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
                CREATE TABLE IF NOT EXISTS query_history (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    sql TEXT DEFAULT '',
                    datasource TEXT DEFAULT '',
                    session_id TEXT DEFAULT '',
                    success BOOLEAN DEFAULT TRUE,
                    row_count INT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_query_history_created
                ON query_history (created_at DESC)
            """)
            await conn.close()
            self._pg_ready = True
            logger.info("query_history 表已就绪")
            return True
        except Exception as e:
            logger.warning("query_history 表创建失败，回退内存模式", error=str(e))
            self._pg_ready = False
            return False

    async def _pg_conn(self):
        """获取 PG 连接，不可用返回 None。"""
        s = get_settings()
        url = s.database_url
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            return await asyncpg.connect(pg_url)
        except Exception:
            return None

    def add(self, user_query: str, datasource: str, session_id: str,
            generated_sql: str = "", success: bool = True, row_count: int = 0) -> None:
        """添加一条查询记录（同步接口，兼容现有调用方）。

        Args:
            user_query - 用户查询语句
            datasource - 数据源名称
            session_id - 会话 ID
            generated_sql - 生成的 SQL
            success - 执行是否成功
            row_count - 返回行数
        """
        item = {
            "id": str(uuid.uuid4())[:12],
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "query": user_query,
            "sql": generated_sql,
            "datasource": datasource,
            "session_id": session_id,
            "success": success,
            "row_count": row_count,
        }
        # 内存环形缓冲
        self._items.append(item)
        if len(self._items) > _MAX_SIZE:
            self._items = self._items[-_MAX_SIZE:]

        # 异步写 PG（fire-and-forget，不阻塞同步调用）
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._pg_insert(item))
        except Exception:
            pass

    async def _pg_insert(self, item: dict) -> None:
        """将条目写入 PG。"""
        if not await self._ensure_pg():
            return
        conn = await self._pg_conn()
        if not conn:
            return
        try:
            await conn.execute(
                "INSERT INTO query_history (id, query, sql, datasource, session_id, success, row_count, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (id) DO NOTHING",
                item["id"], item["query"], item["sql"], item["datasource"],
                item["session_id"], item["success"], item["row_count"],
                datetime.fromisoformat(item["time"]),
            )
        except Exception as e:
            logger.debug("PG 历史写入失败", error=str(e))
        finally:
            await conn.close()

    async def list(self, datasource: str | None = None, search: str | None = None,
                   page: int = 1, page_size: int = 50) -> dict:
        """分页列出查询历史，PG 优先、内存回退。

        Args:
            datasource - 按数据源过滤
            search - 按查询/SQL 关键词搜索
            page - 页码（从 1 开始）
            page_size - 每页条数

        Returns: {"history": [...], "total": int, "page": int, "page_size": int}
        """
        if await self._ensure_pg():
            result = await self._pg_list(datasource, search, page, page_size)
            if result is not None:
                return result

        # 内存回退
        items = self._items
        if datasource:
            items = [r for r in items if r["datasource"] == datasource]
        if search:
            q = search.lower()
            items = [r for r in items if q in r["query"].lower() or q in r["sql"].lower()]
        total = len(items)
        items = list(reversed(items))
        start = (page - 1) * page_size
        return {"history": items[start:start + page_size], "total": total,
                "page": page, "page_size": page_size}

    async def _pg_list(self, datasource: str | None, search: str | None,
                       page: int, page_size: int) -> dict | None:
        """从 PG 分页查询历史。"""
        conn = await self._pg_conn()
        if not conn:
            return None
        try:
            conditions: list[str] = []
            params: list = []
            idx = 1

            if datasource:
                conditions.append(f"datasource = ${idx}")
                params.append(datasource)
                idx += 1
            if search:
                q = f"%{search}%"
                conditions.append(f"(query ILIKE ${idx} OR sql ILIKE ${idx})")
                params.append(q)
                idx += 1

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) FROM query_history {where}", *params)
            total = count_row[0] if count_row else 0

            offset = (page - 1) * page_size
            params.extend([page_size, offset])
            rows = await conn.fetch(
                f"SELECT id, query, sql, datasource, session_id, success, row_count, created_at "
                f"FROM query_history {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
                *params)

            history = [_pg_row_to_dict(r) for r in rows]
            return {"history": history, "total": total, "page": page, "page_size": page_size}
        except Exception as e:
            logger.warning("PG 历史查询失败", error=str(e))
            return None
        finally:
            await conn.close()


def _pg_row_to_dict(row) -> dict:
    """将 PG 行转为前端期望的格式。"""
    return {
        "id": row["id"],
        "time": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row["created_at"] else "",
        "query": row["query"] or "",
        "sql": row["sql"] or "",
        "datasource": row["datasource"] or "",
        "session_id": row["session_id"] or "",
        "success": row["success"] if row["success"] is not None else True,
        "row_count": row["row_count"] or 0,
    }


# 模块级单例
_store: HistoryStore | None = None


def get_history_store() -> HistoryStore:
    """获取 HistoryStore 单例。"""
    global _store
    if _store is None:
        _store = HistoryStore()
    return _store
