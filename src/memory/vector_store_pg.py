"""PgVectorStore — pgvector 实现的 VectorStore。

前置条件:
  1. PG 服务器已安装扩展: CREATE EXTENSION IF NOT EXISTS vector;
  2. pip install pgvector

启用: config.py 中 vector_store_type = "pgvector"
"""

from __future__ import annotations

import asyncio
import json
import time

from src.config import get_settings
from src.db.utils import to_asyncpg_url
from src.logging_config import get_logger
from src.memory.pg_pool import get_pg_pool
from src.memory.vector_store import (
    MetadataFilters,
    VectorEntry,
    VectorSearchResult,
    VectorStore,
    normalize_metadata_filters,
)

logger = get_logger(__name__)
_TABLE = "vector_entries"


class PgVectorStore(VectorStore):
    """pgvector 向量存储。

    嵌入模型复用 all-MiniLM-L6-v2（384 维），与 ChromaDB 一致。
    HNSW 索引保证 < 10K 条目时 < 1ms 查询延迟。
    """

    @classmethod
    async def create(cls, database_url: str) -> "PgVectorStore":
        """建表 + 建索引，返回可用实例。

        Args:
            database_url: PG 连接 URL

        Returns: PgVectorStore 实例
        """
        pg_url = to_asyncpg_url(database_url)
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {_TABLE} (
                        id TEXT PRIMARY KEY, embedding vector(384),
                        content TEXT NOT NULL, metadata JSONB DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW())""")
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_emb "
                    f"ON {_TABLE} USING hnsw (embedding vector_cosine_ops)")
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_meta "
                    f"ON {_TABLE} USING gin (metadata)")
                logger.info("PgVectorStore 初始化完成", table=_TABLE)
            except Exception as exc:
                from src.failure_policy import FailureDomain, must_fail_closed

                must_fail_closed(FailureDomain.DATABASE)
                logger.error(
                    "PgVectorStore 表创建失败，检查 pgvector 扩展",
                    error=str(exc),
                    exc_info=True,
                )
                raise
        store = cls(pg_url)
        await store._get_embed_fn()
        return store

    def __init__(self, pg_url: str):
        """初始化。

        Args:
            pg_url: PG 连接 URL
        """
        self._pg_url = pg_url
        self._embed_fn = None
        self._write_lock = asyncio.Lock()

    async def _get_pool(self):
        """获取应用全局 PostgreSQL 连接池。"""
        logger.debug("PgVectorStore 获取连接池入口")
        pool = await get_pg_pool()
        logger.info("PgVectorStore 获取连接池完成")
        return pool

    # 方法作用：在线程池中延迟加载嵌入模型并缓存 encode 方法。
    # Args: self - 当前 PgVectorStore。
    # Returns: SentenceTransformer encode 可调用对象。
    async def _get_embed_fn(self):
        """延迟加载嵌入模型（单例复用）。

        Returns: callable(text) → list[float]
        """
        if self._embed_fn is None:
            from sentence_transformers import SentenceTransformer
            s = get_settings()
            path = s.embedding_model_path or "all-MiniLM-L6-v2"
            logger.debug("嵌入模型线程池加载入口", path=path)
            model = await asyncio.to_thread(SentenceTransformer, path)
            self._embed_fn = model.encode
            logger.info("嵌入模型加载完成", path=path, dim=384)
        return self._embed_fn

    # 方法作用：在线程池中把文本编码为浮点向量。
    # Args: self - 当前 PgVectorStore；text - 待编码文本。
    # Returns: 嵌入向量浮点列表。
    async def _embed(self, text: str) -> list[float]:
        """计算文本向量。

        Args:
            text: 文本

        Returns: 384 维浮点列表
        """
        fn = await self._get_embed_fn()
        logger.debug("PgVectorStore 文本编码入口", text_length=len(text))
        try:
            encoded = await asyncio.to_thread(fn, text)
            result = encoded.tolist()
        except Exception as exc:
            logger.error("PgVectorStore 文本编码失败", error=str(exc), exc_info=True)
            raise
        logger.info("PgVectorStore 文本编码完成", dimensions=len(result))
        return result

    # ── 检索 ──

    async def search(self, query: str, top_k: int = 5,
                     filters: MetadataFilters | None = None) -> list[VectorSearchResult]:
        """语义向量搜索，cosine distance 转 score。

        Args:
            query: 查询文本
            top_k: 返回上限
            filters: metadata 过滤

        Returns: VectorSearchResult 列表
        """
        _start = time.monotonic()
        top_k = min(top_k, 50)
        emb = await self._embed(query)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if filters:
                clause, filter_values = _build_filter_clause(filters, parameter_start=2)
                limit_parameter = len(filter_values) + 2
                rows = await conn.fetch(
                    f"SELECT id, content, metadata, 1-(embedding <=> $1) AS score "
                    f"FROM {_TABLE} WHERE {clause} "
                    f"ORDER BY embedding <=> $1 LIMIT ${limit_parameter}",
                    emb, *filter_values, top_k)
            else:
                rows = await conn.fetch(
                    f"SELECT id, content, metadata, 1-(embedding <=> $1) AS score "
                    f"FROM {_TABLE} ORDER BY embedding <=> $1 LIMIT $2",
                    emb, top_k)
            results = [_row_to_result(r) for r in rows]
            logger.debug("PgVectorStore search", hits=len(results),
                         elapsed_ms=round((time.monotonic() - _start) * 1000))
            return results

    async def get_by_id(self, entry_id: str) -> VectorEntry | None:
        """按 ID 获取。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT id, content, metadata FROM {_TABLE} WHERE id=$1", entry_id)
            return _row_to_entry(row) if row else None

    async def get_by_filter(self, filters: MetadataFilters,
                            limit: int = 100) -> list[VectorEntry]:
        """按 metadata 精确过滤。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            clause, filter_values = _build_filter_clause(filters)
            limit_parameter = len(filter_values) + 1
            rows = await conn.fetch(
                f"SELECT id, content, metadata FROM {_TABLE} "
                f"WHERE {clause} LIMIT ${limit_parameter}",
                *filter_values, limit)
            return [_row_to_entry(r) for r in rows]

    # ── 写入 ──

    async def upsert(self, entries: list[VectorEntry]) -> int:
        """批量插入/更新。"""
        if not entries:
            return 0
        async with self._write_lock:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                count = 0
                for e in entries:
                    emb = e.embedding or await self._embed(e.content)
                    await conn.execute(
                        f"INSERT INTO {_TABLE} (id, embedding, content, metadata) "
                        f"VALUES ($1, $2, $3, $4::jsonb) "
                        f"ON CONFLICT (id) DO UPDATE SET content=EXCLUDED.content, "
                        f"metadata=EXCLUDED.metadata, embedding=EXCLUDED.embedding, updated_at=NOW()",
                        e.id, emb, e.content, json.dumps(e.metadata, ensure_ascii=False))
                    count += 1
                logger.debug("PgVectorStore upsert 完成", count=count)
                return count

    async def delete_by_ids(self, ids: list[str]) -> int:
        """按 ID 删除。"""
        if not ids:
            return 0
        async with self._write_lock:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                r = await conn.execute(f"DELETE FROM {_TABLE} WHERE id = ANY($1)", ids)
                return int(r.split()[-1]) if r else 0

    async def delete_by_filter(self, filters: MetadataFilters) -> int:
        """按 metadata 过滤删除。"""
        logger.debug("PgVectorStore metadata 删除入口", filter_count=len(filters))
        if not filters:
            logger.warning("PgVectorStore 空过滤删除已阻断")
            logger.info("PgVectorStore metadata 删除完成", count=0)
            return 0
        async with self._write_lock:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                clause, filter_values = _build_filter_clause(filters)
                r = await conn.execute(
                    f"DELETE FROM {_TABLE} WHERE {clause}", *filter_values)
                result = int(r.split()[-1]) if r else 0
        logger.info("PgVectorStore metadata 删除完成", count=result)
        return result

    # ── 管理 ──

    async def count(self, filters: MetadataFilters | None = None) -> int:
        """条目总数。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if filters:
                clause, filter_values = _build_filter_clause(filters)
                row = await conn.fetchrow(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE {clause}",
                    *filter_values)
            else:
                row = await conn.fetchrow(f"SELECT COUNT(*) FROM {_TABLE}")
            return row[0] if row else 0

    async def health_check(self) -> bool:
        """验证 PG 连通性 + pgvector 类型可用。"""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
                await conn.fetchval("SELECT '[1,0]'::vector")
            return True
        except Exception:
            logger.error("PgVectorStore 健康检查失败", exc_info=True)
            return False


# 方法作用：把统一 metadata 过滤条件编译为参数化 PostgreSQL JSONB 谓词。
# Args: filters - 公共过滤 DSL；parameter_start - 第一个 SQL 参数序号。
# Returns: SQL 条件片段和按序绑定的 JSON 字符串。
def _build_filter_clause(
    filters: MetadataFilters,
    *,
    parameter_start: int = 1,
) -> tuple[str, list[str]]:
    logger.debug(
        "构建 pgvector metadata 条件入口",
        filter_count=len(filters),
        parameter_start=parameter_start,
    )
    try:
        clauses: list[str] = []
        values: list[str] = []
        for offset, (key, is_not, value) in enumerate(
            normalize_metadata_filters(filters),
        ):
            parameter = parameter_start + offset
            predicate = f"metadata @> ${parameter}::jsonb"
            clauses.append(f"NOT ({predicate})" if is_not else predicate)
            values.append(json.dumps({key: value}, ensure_ascii=False))
        result = " AND ".join(clauses) if clauses else "TRUE"
    except Exception as exc:
        logger.error("构建 pgvector metadata 条件失败", error=str(exc), exc_info=True)
        raise
    logger.info("构建 pgvector metadata 条件完成", condition_count=len(clauses))
    return result, values


def _row_to_result(r) -> VectorSearchResult:
    meta = r["metadata"]
    return VectorSearchResult(
        id=r["id"], content=r["content"],
        metadata=json.loads(meta) if isinstance(meta, str) else (meta or {}),
        score=round(float(r["score"]), 4))


def _row_to_entry(r) -> VectorEntry:
    meta = r["metadata"]
    return VectorEntry(id=r["id"], content=r["content"],
                       metadata=json.loads(meta) if isinstance(meta, str) else (meta or {}))
