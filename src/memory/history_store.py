"""查询历史存储 — 内存环形缓冲区 + PG 持久化。

PG 可用时优先读写，不可用时回退内存模式。
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
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
        """确保包含身份列的 PG 表存在。

        Returns:
            PostgreSQL 可用且表结构就绪时返回 True。
        """
        logger.debug("历史 PG 初始化入口", cached=self._pg_ready)
        if self._pg_ready is not None:
            logger.info("历史 PG 初始化命中缓存", ready=self._pg_ready)
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
                    user_id INT NOT NULL DEFAULT 0,
                    tenant_id INT NOT NULL DEFAULT 1,
                    success BOOLEAN DEFAULT TRUE,
                    row_count INT DEFAULT 0,
                    final_result JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_query_history_created
                ON query_history (created_at DESC)
            """)
            await conn.execute("ALTER TABLE query_history ADD COLUMN IF NOT EXISTS user_id INT NOT NULL DEFAULT 0")
            await conn.execute("ALTER TABLE query_history ADD COLUMN IF NOT EXISTS tenant_id INT NOT NULL DEFAULT 1")
            await conn.execute(
                "ALTER TABLE query_history ADD COLUMN IF NOT EXISTS "
                "final_result JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_query_history_identity_created "
                "ON query_history (tenant_id, user_id, created_at DESC)")
            await conn.close()
            self._pg_ready = True
            logger.info("query_history 表已就绪")
            return True
        except Exception as exc:
            logger.error("query_history 表创建失败，回退内存模式", error=str(exc), exc_info=True)
            self._pg_ready = False
            return False

    async def _pg_conn(self):
        """获取注入当前身份参数的 PG 连接。

        Returns:
            可用的 asyncpg 连接；不可用返回 None。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("历史 PG 连接入口", user_id=user_id, tenant_id=tenant_id)
        s = get_settings()
        url = s.database_url
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            await conn.execute(
                "SELECT set_config('app.current_user_id', $1, false), "
                "set_config('app.current_tenant_id', $2, false)",
                str(user_id), str(tenant_id),
            )
            logger.info("历史 PG 连接完成", user_id=user_id, tenant_id=tenant_id)
            return conn
        except Exception as exc:
            logger.error("历史 PG 连接失败", error=str(exc), exc_info=True)
            return None

    def add(self, user_query: str, datasource: str, session_id: str,
            generated_sql: str = "", success: bool = True, row_count: int = 0,
            final_result: dict | None = None) -> None:
        """添加一条绑定当前身份的查询记录（同步接口，兼容现有调用方）。

        Args:
            user_query - 用户查询语句
            datasource - 数据源名称
            session_id - 会话 ID
            generated_sql - 生成的 SQL
            success - 执行是否成功
            row_count - 返回行数
            final_result - 本轮完整结构化响应
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("添加历史入口", session_id=session_id[:20], user_id=user_id, tenant_id=tenant_id)
        item = {
            "id": str(uuid.uuid4())[:12],
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "query": user_query,
            "sql": generated_sql,
            "datasource": datasource,
            "session_id": session_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "success": success,
            "row_count": row_count,
            "final_result": deepcopy(final_result or {}),
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
            logger.error("历史异步任务创建失败", session_id=session_id[:20], exc_info=True)
        logger.info("历史已加入内存", session_id=session_id[:20], user_id=user_id)

    async def _pg_insert(self, item: dict) -> None:
        """将条目写入 PG。"""
        if not await self._ensure_pg():
            return
        conn = await self._pg_conn()
        if not conn:
            return
        try:
            from src.api.streaming import _PrecisionEncoder, _json_serialize

            await conn.execute(
                "INSERT INTO query_history "
                "(id, query, sql, datasource, session_id, user_id, tenant_id, success, "
                "row_count, final_result, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11) "
                "ON CONFLICT (id) DO NOTHING",
                item["id"], item["query"], item["sql"], item["datasource"],
                item["session_id"], item["user_id"], item["tenant_id"],
                item["success"], item["row_count"],
                json.dumps(
                    item["final_result"], cls=_PrecisionEncoder,
                    default=_json_serialize, ensure_ascii=False,
                ),
                datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error("PG 历史写入失败", error=str(exc), exc_info=True)
        finally:
            await conn.close()

    async def list_session(
        self, session_id: str, before: int | None = None, limit: int = 20,
    ) -> list[dict]:
        """按会话读取查询历史，供 Checkpointer 不可用时恢复对话轮次。

        Args:
            session_id: 对外会话 ID。
            before: 只返回该轮次之前的记录。
            limit: 最大返回记录数。

        Returns:
            按时间正序排列的查询历史记录，记录包含 turn_id。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug(
            "按会话读取历史入口", session_id=session_id[:20],
            user_id=user_id, tenant_id=tenant_id, before=before, limit=limit,
        )
        items: list[dict] = []
        if await self._ensure_pg():
            conn = await self._pg_conn()
            if conn:
                try:
                    rows = await conn.fetch(
                        "SELECT id, query, sql, datasource, session_id, success, row_count, "
                        "final_result, created_at "
                        "FROM query_history WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3 "
                        "ORDER BY created_at ASC LIMIT $4",
                        tenant_id, user_id, session_id, max(limit, 1000),
                    )
                    items = [_pg_row_to_dict(row) for row in rows]
                    logger.info(
                        "按会话读取历史完成", source="postgres",
                        session_id=session_id[:20], count=len(items),
                    )
                except Exception as exc:
                    logger.error("按会话读取历史失败", error=str(exc), exc_info=True)
                finally:
                    await conn.close()

        memory_items = [
            dict(item) for item in self._items
            if item.get("session_id") == session_id
            and item.get("tenant_id") == tenant_id
            and item.get("user_id") == user_id
        ]
        if items:
            # 异步 PG 写入尚未完成时合并内存记录，避免刚结束的轮次短暂消失。
            persisted_ids = {str(item.get("id", "")) for item in items}
            items.extend(
                item for item in memory_items
                if str(item.get("id", "")) not in persisted_ids
            )
            logger.info(
                "按会话历史内存补偿完成", session_id=session_id[:20],
                memory_count=len(memory_items), merged_count=len(items),
            )
        else:
            items = [
                dict(item) for item in self._items
                if item.get("session_id") == session_id
                and item.get("tenant_id") == tenant_id
                and item.get("user_id") == user_id
            ]
            items.reverse()
            logger.info(
                "按会话读取历史完成", source="memory",
                session_id=session_id[:20], count=len(items),
            )

        items.sort(key=lambda item: item.get("time", ""))
        for turn_id, item in enumerate(items, 1):
            item["turn_id"] = turn_id
        candidates = items[: before - 1] if before is not None else items
        result = candidates[-limit:]
        logger.info(
            "按会话历史裁剪完成", session_id=session_id[:20], count=len(result),
        )
        return result

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
        from src.api.auth import get_current_tenant_id, get_current_user_id

        user_id = get_current_user_id()
        tenant_id = get_current_tenant_id()
        logger.debug("列出历史入口", user_id=user_id, tenant_id=tenant_id, page=page, page_size=page_size)
        if await self._ensure_pg():
            result = await self._pg_list(datasource, search, page, page_size)
            if result is not None:
                return result

        # 内存回退
        items = [
            item for item in self._items
            if item.get("user_id") == user_id and item.get("tenant_id") == tenant_id
        ]
        if datasource:
            items = [r for r in items if r["datasource"] == datasource]
        if search:
            q = search.lower()
            items = [r for r in items if q in r["query"].lower() or q in r["sql"].lower()]
        total = len(items)
        items = list(reversed(items))
        start = (page - 1) * page_size
        result = {"history": items[start:start + page_size], "total": total,
                  "page": page, "page_size": page_size}
        logger.info("列出历史完成", source="memory", count=len(result["history"]), user_id=user_id)
        return result

    async def _pg_list(self, datasource: str | None, search: str | None,
                       page: int, page_size: int) -> dict | None:
        """从 PG 分页查询历史。"""
        conn = await self._pg_conn()
        if not conn:
            return None
        try:
            from src.api.auth import get_current_tenant_id, get_current_user_id

            conditions: list[str] = ["tenant_id = $1", "user_id = $2"]
            params: list = [get_current_tenant_id(), get_current_user_id()]
            idx = 3

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
                f"SELECT id, query, sql, datasource, session_id, success, row_count, "
                f"final_result, created_at "
                f"FROM query_history {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
                *params)

            history = [_pg_row_to_dict(r) for r in rows]
            logger.info("列出历史完成", source="postgres", count=len(history), user_id=params[1])
            return {"history": history, "total": total, "page": page, "page_size": page_size}
        except Exception as exc:
            logger.error("PG 历史查询失败", error=str(exc), exc_info=True)
            return None
        finally:
            await conn.close()


def _pg_row_to_dict(row) -> dict:
    """将 PG 行转为前端期望的格式。"""
    raw_final_result = row.get("final_result", {})
    if isinstance(raw_final_result, str):
        try:
            raw_final_result = json.loads(raw_final_result)
        except json.JSONDecodeError:
            logger.warning("历史结构化响应解析失败", record_id=row["id"])
            raw_final_result = {}
    return {
        "id": row["id"],
        "time": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row["created_at"] else "",
        "query": row["query"] or "",
        "sql": row["sql"] or "",
        "datasource": row["datasource"] or "",
        "session_id": row["session_id"] or "",
        "success": row["success"] if row["success"] is not None else True,
        "row_count": row["row_count"] or 0,
        "final_result": raw_final_result if isinstance(raw_final_result, dict) else {},
    }


# 模块级单例
_store: HistoryStore | None = None


def get_history_store() -> HistoryStore:
    """获取 HistoryStore 单例。"""
    global _store
    if _store is None:
        _store = HistoryStore()
    return _store
