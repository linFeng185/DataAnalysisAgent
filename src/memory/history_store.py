"""查询历史存储 — 内存环形缓冲区。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.logging_config import get_logger

logger = get_logger(__name__)

_MAX_SIZE = 500


class HistoryStore:
    """内存中的查询历史，自动截断到最近 500 条。"""

    def __init__(self) -> None:
        self._items: list[dict] = []

    def add(self, user_query: str, datasource: str, session_id: str,
            generated_sql: str = "", success: bool = True, row_count: int = 0) -> None:
        self._items.append({
            "id": str(uuid.uuid4())[:12],
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "query": user_query,
            "sql": generated_sql,
            "datasource": datasource,
            "session_id": session_id,
            "success": success,
            "row_count": row_count,
        })
        if len(self._items) > _MAX_SIZE:
            self._items = self._items[-_MAX_SIZE:]

    def list(self, datasource: str | None = None, search: str | None = None) -> list[dict]:
        result = self._items
        if datasource:
            result = [r for r in result if r["datasource"] == datasource]
        if search:
            q = search.lower()
            result = [r for r in result if q in r["query"].lower() or q in r["sql"].lower()]
        return list(reversed(result))  # 最新在前


# 模块级单例
_store: HistoryStore | None = None


def get_history_store() -> HistoryStore:
    global _store
    if _store is None:
        _store = HistoryStore()
    return _store
