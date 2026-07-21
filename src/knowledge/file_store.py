"""知识库文件存储：PostgreSQL bytea 与受控磁盘回退。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection

logger = get_logger(__name__)

_VISIBLE_FILES_SQL = """
    knowledge_scope = 'system'
    OR (knowledge_scope = 'tenant' AND tenant_id = {tenant_param})
    OR (knowledge_scope = 'private' AND tenant_id = {tenant_param} AND user_id = {user_param})
"""


def _current_identity() -> tuple[int, int]:
    """读取当前请求的租户和用户身份。

    Returns:
        `(tenant_id, user_id)` 二元组。
    """
    from src.api.auth import get_current_tenant_id, get_current_user_id

    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    logger.debug("读取知识文件身份", tenant_id=tenant_id, user_id=user_id)
    return tenant_id, user_id


class FileStore:
    """将上传的原始文件按租户和用户存入 PostgreSQL。"""

    def __init__(self) -> None:
        self._ready = False

    async def _ensure(self) -> None:
        """确保知识文件表和身份字段存在。

        Returns:
            无返回值；数据库不可用时启用磁盘回退。
        """
        logger.debug("知识文件 PG 初始化入口", ready=self._ready)
        if self._ready:
            logger.info("知识文件 PG 初始化命中缓存", ready=True)
            return
        settings = get_settings()
        url = settings.database_url
        if not url or "postgres" not in url:
            self._ready = True
            logger.info("知识文件启用磁盘回退", reason="PostgreSQL 未配置")
            return

        try:
            async with pg_connection(tenant_id=1, user_id=0, role="super_admin") as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_files (
                        id SERIAL PRIMARY KEY,
                        filename TEXT NOT NULL,
                        content_type TEXT DEFAULT 'application/octet-stream',
                        file_data BYTEA NOT NULL,
                        size BIGINT DEFAULT 0,
                        user_id INT NOT NULL DEFAULT 0,
                        tenant_id INT NOT NULL DEFAULT 1,
                        knowledge_scope VARCHAR(16) NOT NULL DEFAULT 'private',
                        datasource VARCHAR(128) NOT NULL DEFAULT '',
                        tag_ids BIGINT[] NOT NULL DEFAULT '{}',
                        uploaded_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS user_id INT NOT NULL DEFAULT 0")
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS tenant_id INT NOT NULL DEFAULT 1")
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "knowledge_scope VARCHAR(16) NOT NULL DEFAULT 'private'")
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "datasource VARCHAR(128) NOT NULL DEFAULT ''")
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "tag_ids BIGINT[] NOT NULL DEFAULT '{}'")
                await conn.execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'ck_knowledge_files_scope') THEN "
                    "ALTER TABLE knowledge_files ADD CONSTRAINT ck_knowledge_files_scope "
                    "CHECK (knowledge_scope IN ('system', 'tenant', 'private')); "
                    "END IF; END $$")
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_knowledge_files_scope_identity_uploaded "
                    "ON knowledge_files (knowledge_scope, tenant_id, user_id, uploaded_at DESC)")
            self._ready = True
            logger.info("knowledge_files 表已就绪")
        except Exception as exc:
            logger.error("knowledge_files 创建失败，降级磁盘", error=str(exc), exc_info=True)
            self._ready = True
    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any | None]:
        """创建带当前 RLS 参数的 PostgreSQL 连接。

        Returns:
            已设置身份上下文的连接；连接失败返回 None。
        """
        from src.api.auth import get_current_role
        from src.knowledge.governance import normalize_role

        tenant_id, user_id = _current_identity()
        role = normalize_role(get_current_role())
        settings = get_settings()
        url = settings.database_url
        logger.debug("知识文件 PG 连接入口", tenant_id=tenant_id, user_id=user_id)
        if not url or "postgres" not in url:
            logger.info("知识文件 PG 连接回退", reason="PostgreSQL 未配置")
            yield None
            return
        try:
            async with pg_connection(
                tenant_id=tenant_id,
                user_id=user_id,
                role=role,
            ) as conn:
                yield conn
            logger.info("知识文件 PG 连接完成", tenant_id=tenant_id, user_id=user_id, role=role)
        except Exception as exc:
            logger.error("知识文件 PG 连接失败", error=str(exc), exc_info=True)
            raise

    async def save(
        self,
        filename: str,
        content: bytes,
        *,
        knowledge_scope: str = "private",
        datasource: str = "",
        tag_ids: list[int] | None = None,
    ) -> int | None:
        """按当前身份、知识范围和标签保存原始文件。

        Args:
            filename: 原始文件名。
            content: 文件二进制内容。
            knowledge_scope: system/tenant/private 知识范围。
            datasource: 可选绑定的数据源名称。
            tag_ids: 已校验的标签 ID。

        Returns:
            文件 ID；数据库不可用或保存失败返回 None。
        """
        from src.api.auth import get_current_role
        from src.knowledge.governance import normalize_role
        from src.knowledge.governance import can_write_knowledge_scope, normalize_knowledge_scope

        tenant_id, user_id = _current_identity()
        role = normalize_role(get_current_role())
        normalized_scope = normalize_knowledge_scope(knowledge_scope).value
        logger.debug(
            "保存知识文件入口",
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            knowledge_scope=normalized_scope,
        )
        if not can_write_knowledge_scope(
            normalized_scope,
            role=role,
            user_id=user_id,
            multi_tenant=getattr(get_settings(), "multi_tenant", False),
        ):
            logger.warning(
                "保存知识文件权限拒绝",
                filename=filename,
                role=role,
                knowledge_scope=normalized_scope,
            )
            raise PermissionError(f"当前角色无权写入 {normalized_scope} 知识")
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    logger.info("保存知识文件回退", filename=filename, reason="PG 不可用")
                    return None
                ext = os.path.splitext(filename)[1].lower()
                row = await conn.fetchrow(
                    "INSERT INTO knowledge_files "
                    "(filename, content_type, file_data, size, tenant_id, user_id, "
                    "knowledge_scope, datasource, tag_ids) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
                    filename, _content_type(ext), content, len(content), tenant_id, user_id,
                    normalized_scope, datasource.strip(), list(tag_ids or []),
                )
                result = row["id"] if row else None
                logger.info("保存知识文件完成", file_id=result, tenant_id=tenant_id, user_id=user_id)
                return result
        except Exception as exc:
            logger.error("PG 保存失败", error=str(exc), exc_info=True)
            return None

    async def get(self, file_id: int) -> dict | None:
        """按当前租户和用户读取文件。

        Args:
            file_id: 文件 ID。

        Returns:
            可见文件字典；不存在或无权访问返回 None。
        """
        tenant_id, user_id = _current_identity()
        logger.debug("读取知识文件入口", file_id=file_id, tenant_id=tenant_id, user_id=user_id)
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    return None
                row = await conn.fetchrow(
                    "SELECT id, filename, content_type, file_data, size, uploaded_at, "
                    "knowledge_scope, datasource, tag_ids, tenant_id, user_id "
                    "FROM knowledge_files WHERE id = $1 AND ("
                    + _VISIBLE_FILES_SQL.format(tenant_param="$2", user_param="$3") + ")",
                    file_id, tenant_id, user_id,
                )
                if not row:
                    logger.info("读取知识文件完成", found=False, file_id=file_id, user_id=user_id)
                    return None
                result = {
                    "id": row["id"],
                    "filename": row["filename"],
                    "content_type": row["content_type"],
                    "file_data": row["file_data"],
                    "size": row["size"],
                    "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else "",
                    "scope": row["knowledge_scope"],
                    "datasource": row["datasource"],
                    "tag_ids": list(row["tag_ids"] or []),
                    "tenant_id": row["tenant_id"],
                    "owner_user_id": row["user_id"],
                }
                logger.info("读取知识文件完成", found=True, file_id=file_id, user_id=user_id)
                return result
        except Exception as exc:
            logger.error("读取知识文件失败", error=str(exc), exc_info=True)
            return None

    async def get_by_name(
        self,
        filename: str,
        *,
        knowledge_scope: str = "",
    ) -> dict | None:
        """按当前租户和用户读取最新同名文件。

        Args:
            filename: 原始文件名。
            knowledge_scope: 可选知识范围，用于区分同名文档。

        Returns:
            可见文件字典；不存在返回 None。
        """
        tenant_id, user_id = _current_identity()
        logger.debug(
            "按名称读取知识文件入口",
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            knowledge_scope=knowledge_scope,
        )
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    return None
                row = await conn.fetchrow(
                    "SELECT id, filename, content_type, file_data, size, uploaded_at, "
                    "knowledge_scope, datasource, tag_ids, tenant_id, user_id "
                    "FROM knowledge_files WHERE filename = $1 AND ("
                    + _VISIBLE_FILES_SQL.format(tenant_param="$2", user_param="$3")
                    + ") AND ($4 = '' OR knowledge_scope = $4) "
                    + "ORDER BY uploaded_at DESC LIMIT 1",
                    filename, tenant_id, user_id, knowledge_scope,
                )
                result = None
                if row:
                    result = {
                        "id": row["id"],
                        "filename": row["filename"],
                        "content_type": row["content_type"],
                        "file_data": row["file_data"],
                        "size": row["size"],
                        "scope": row["knowledge_scope"],
                        "datasource": row["datasource"],
                        "tag_ids": list(row["tag_ids"] or []),
                        "tenant_id": row["tenant_id"],
                        "owner_user_id": row["user_id"],
                    }
                logger.info("按名称读取知识文件完成", found=result is not None, filename=filename)
                return result
        except Exception as exc:
            logger.error("按名称读取知识文件失败", error=str(exc), exc_info=True)
            return None

    async def list_files(self) -> list[dict]:
        """列出当前租户和用户可见的知识文件。

        Returns:
            文件摘要列表。
        """
        tenant_id, user_id = _current_identity()
        logger.debug("列出知识文件入口", tenant_id=tenant_id, user_id=user_id)
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    result = _disk_list()
                    logger.info("列出知识文件完成", source="disk", count=len(result), user_id=user_id)
                    return result
                rows = await conn.fetch(
                    "SELECT id, filename, size, uploaded_at, knowledge_scope, datasource, "
                    "tag_ids, tenant_id, user_id FROM knowledge_files "
                    "WHERE ("
                    + _VISIBLE_FILES_SQL.format(tenant_param="$1", user_param="$2")
                    + ") ORDER BY uploaded_at DESC",
                    tenant_id, user_id,
                )
                result = [
                    {
                        "id": row["id"],
                        "name": row["filename"],
                        "size": row["size"],
                        "modified": row["uploaded_at"].timestamp() if row["uploaded_at"] else 0,
                        "scope": row["knowledge_scope"],
                        "datasource": row["datasource"],
                        "tag_ids": list(row["tag_ids"] or []),
                        "tenant_id": row["tenant_id"],
                        "owner_user_id": row["user_id"],
                        "is_builtin": False,
                    }
                    for row in rows
                ]
                logger.info("列出知识文件完成", source="postgres", count=len(result), user_id=user_id)
                return result
        except Exception as exc:
            logger.error("列出知识文件失败", error=str(exc), exc_info=True)
            return _disk_list()

    async def delete(self, filename: str, *, knowledge_scope: str = "") -> bool:
        """删除当前租户和用户拥有的同名文件。

        Args:
            filename: 原始文件名。
            knowledge_scope: 可选知识范围，用于区分同名文档。

        Returns:
            至少删除一条可见记录时返回 True。
        """
        from src.api.auth import get_current_role
        from src.knowledge.governance import normalize_role

        tenant_id, user_id = _current_identity()
        role = normalize_role(get_current_role())
        logger.debug(
            "删除知识文件入口",
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            knowledge_scope=knowledge_scope,
        )
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    logger.info("删除知识文件回退", filename=filename, reason="PG 不可用")
                    return False
                status = await conn.execute(
                    "DELETE FROM knowledge_files WHERE filename = $1 AND ("
                    "($4 = 'super_admin' AND knowledge_scope = 'system') OR "
                    "(($4 IN ('super_admin', 'tenant_admin')) AND knowledge_scope = 'tenant' AND tenant_id = $2) OR "
                    "(knowledge_scope = 'private' AND tenant_id = $2 AND user_id = $3)"
                    ") AND ($5 = '' OR knowledge_scope = $5)",
                    filename, tenant_id, user_id, role, knowledge_scope,
                )
                deleted = not status.endswith(" 0")
                logger.info("删除知识文件完成", filename=filename, deleted=deleted, user_id=user_id)
                return deleted
        except Exception as exc:
            logger.error("删除知识文件失败", error=str(exc), exc_info=True)
            return False


def _content_type(ext: str) -> str:
    """根据扩展名获取文件 MIME 类型。

    Args:
        ext: 小写扩展名。

    Returns:
        MIME 类型字符串。
    """
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(ext, "application/octet-stream")


def _disk_list() -> list[dict]:
    """列出内置指标目录中的文档。

    Returns:
        内置文档摘要列表。
    """
    docs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "metrics")
    docs = []
    if os.path.isdir(docs_dir):
        for filename in sorted(os.listdir(docs_dir)):
            if any(filename.endswith(ext) for ext in (".md", ".txt", ".pdf", ".docx", ".doc")):
                path = os.path.join(docs_dir, filename)
                docs.append({
                    "name": filename,
                    "size": os.path.getsize(path),
                    "modified": os.path.getmtime(path),
                    "scope": "system",
                    "datasource": "",
                    "tag_ids": [],
                    "is_builtin": True,
                })
    return docs


_store: FileStore | None = None


def get_file_store() -> FileStore:
    """获取 FileStore 单例。

    Returns:
        模块级 FileStore 实例。
    """
    global _store
    if _store is None:
        _store = FileStore()
    return _store
