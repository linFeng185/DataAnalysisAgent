"""知识库文件存储 — PostgreSQL bytea 替代磁盘文件。"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)


class FileStore:
    """将上传的原始文件存入 PostgreSQL，替代 docs/metrics/ 磁盘存储。"""

    def __init__(self) -> None:
        self._ready = False

    async def _ensure(self):
        if self._ready:
            return
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            self._ready = True
            return
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_files (
                    id SERIAL PRIMARY KEY,
                    filename TEXT NOT NULL,
                    content_type TEXT DEFAULT 'application/octet-stream',
                    file_data BYTEA NOT NULL,
                    size BIGINT DEFAULT 0,
                    uploaded_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.close()
            self._ready = True
            logger.info("knowledge_files 表已就绪")
        except Exception as e:
            logger.warning("knowledge_files 创建失败，降级磁盘", error=str(e))
            self._ready = True

    async def save(self, filename: str, content: bytes) -> int | None:
        """保存到 PG，返回文件 ID。PG 不可用返回 None。"""
        await self._ensure()
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            return None
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            ext = os.path.splitext(filename)[1].lower()
            ctype = _content_type(ext)
            row = await conn.fetchrow(
                "INSERT INTO knowledge_files (filename, content_type, file_data, size) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                filename, ctype, content, len(content))
            await conn.close()
            return row["id"]
        except Exception as e:
            logger.warning("PG 保存失败", error=str(e))
            return None

    async def get(self, file_id: int) -> dict | None:
        await self._ensure()
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            return None
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            row = await conn.fetchrow(
                "SELECT id, filename, content_type, file_data, size, uploaded_at "
                "FROM knowledge_files WHERE id = $1", file_id)
            await conn.close()
            if row:
                return {"id": row["id"], "filename": row["filename"],
                        "content_type": row["content_type"],
                        "file_data": row["file_data"], "size": row["size"],
                        "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else ""}
        except Exception:
            pass
        return None

    async def get_by_name(self, filename: str) -> dict | None:
        await self._ensure()
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            return None
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            row = await conn.fetchrow(
                "SELECT id, filename, content_type, file_data, size, uploaded_at "
                "FROM knowledge_files WHERE filename = $1 ORDER BY uploaded_at DESC LIMIT 1",
                filename)
            await conn.close()
            return row and {"id": row["id"], "filename": row["filename"],
                            "content_type": row["content_type"],
                            "file_data": row["file_data"], "size": row["size"]} or None
        except Exception:
            return None

    async def list_files(self) -> list[dict]:
        await self._ensure()
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            return _disk_list()
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            rows = await conn.fetch(
                "SELECT id, filename, size, uploaded_at FROM knowledge_files ORDER BY uploaded_at DESC")
            await conn.close()
            return [{"id": r["id"], "name": r["filename"], "size": r["size"],
                     "modified": r["uploaded_at"].timestamp() if r["uploaded_at"] else 0,
                     "is_builtin": False} for r in rows]
        except Exception:
            return _disk_list()

    async def delete(self, filename: str) -> bool:
        await self._ensure()
        s = get_settings()
        url = s.database_url
        if not url or "postgres" not in url:
            return False
        try:
            import asyncpg
            pg_url = url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(pg_url)
            await conn.execute("DELETE FROM knowledge_files WHERE filename = $1", filename)
            await conn.close()
            return True
        except Exception:
            return False


def _content_type(ext: str) -> str:
    return {".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword", ".txt": "text/plain",
            ".md": "text/markdown"}.get(ext, "application/octet-stream")


def _disk_list() -> list[dict]:
    import os
    docs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "metrics")
    docs = []
    if os.path.isdir(docs_dir):
        for f in sorted(os.listdir(docs_dir)):
            if any(f.endswith(e) for e in (".md", ".txt", ".pdf", ".docx", ".doc")):
                fp = os.path.join(docs_dir, f)
                docs.append({"name": f, "size": os.path.getsize(fp),
                             "modified": os.path.getmtime(fp), "is_builtin": True})
    return docs


_store = None


def get_file_store() -> FileStore:
    global _store
    if _store is None:
        _store = FileStore()
    return _store
