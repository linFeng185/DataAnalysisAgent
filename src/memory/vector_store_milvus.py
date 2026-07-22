"""MilvusVectorStore — Milvus 实现的 VectorStore。

前置条件: Docker: docker run -d --name milvus -p 19530:19530 milvusdb/milvus:latest
          pip install pymilvus
启用: config.py 中 vector_store_type = "milvus"
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.vector_store import VectorEntry, VectorSearchResult, VectorStore

logger = get_logger(__name__)
_COLLECTION = "data_agent_knowledge"
_DIM = 384


class MilvusVectorStore(VectorStore):
    """Milvus 向量存储。嵌入复用 all-MiniLM-L6-v2（384维）。"""

    @classmethod
    async def create(cls, uri: str) -> "MilvusVectorStore":
        """连接到 Milvus，创建 Collection（幂等）。

        Args:
            uri: Milvus 地址，如 http://localhost:19530

        Returns: MilvusVectorStore 实例
        """
        from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility

        connections.connect("default", uri=uri, timeout=10)
        logger.info("Milvus 已连接", uri=uri)

        if not utility.has_collection(_COLLECTION):
            id_f = FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128)
            emb_f = FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=_DIM)
            cont_f = FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535)
            meta_f = FieldSchema(name="metadata", dtype=DataType.VARCHAR, max_length=65535)
            schema = CollectionSchema(fields=[id_f, emb_f, cont_f, meta_f])
            col = Collection(name=_COLLECTION, schema=schema)
            col.create_index(field_name="embedding", index_params={
                "metric_type": "COSINE", "index_type": "IVF_FLAT", "params": {"nlist": 128}})
            logger.info("Milvus Collection 已创建", name=_COLLECTION, dim=_DIM)
        else:
            logger.info("Milvus Collection 已存在", name=_COLLECTION)

        store = cls()
        await store._get_embed_fn()
        return store

    def __init__(self):
        self._embed_fn = None
        self._write_lock = asyncio.Lock()

    def _get_collection(self):
        from pymilvus import Collection
        col = Collection(name=_COLLECTION)
        col.load()
        return col

    async def _get_embed_fn(self):
        if self._embed_fn is None:
            from sentence_transformers import SentenceTransformer
            s = get_settings()
            path = s.embedding_model_path or "all-MiniLM-L6-v2"
            self._embed_fn = SentenceTransformer(path).encode
            logger.info("嵌入模型加载完成", path=path, dim=_DIM)
        return self._embed_fn

    async def _embed(self, text: str) -> list[float]:
        return (await self._get_embed_fn())(text).tolist()

    def _to_expr(self, filters: dict[str, Any] | None) -> str | None:
        """把 metadata 等值条件转换为安全的 Milvus 候选过滤表达式。

        Args:
            filters: metadata 精确过滤条件。

        Returns:
            Milvus LIKE 表达式；无正向条件时返回 None。
        """
        logger.debug("构建 Milvus metadata 表达式入口", filter_count=len(filters or {}))
        if not filters:
            logger.info("构建 Milvus metadata 表达式完成", has_expression=False)
            return None
        parts: list[str] = []
        for key, value in filters.items():
            if key.startswith("not:") or (isinstance(value, dict) and "$ne" in value):
                continue
            pattern = (
                f"%{json.dumps(key, ensure_ascii=False)}: "
                f"{json.dumps(value, ensure_ascii=False)}%"
            )
            parts.append(f"metadata like {json.dumps(pattern, ensure_ascii=False)}")
        result = " && ".join(parts) if parts else None
        logger.info("构建 Milvus metadata 表达式完成", has_expression=bool(result))
        return result

    @staticmethod
    def _metadata_matches(metadata: dict[str, Any], filters: dict[str, Any] | None) -> bool:
        """对 Milvus 候选记录执行 metadata 精确匹配。

        Args:
            metadata: 记录的 metadata。
            filters: 等值或不等值过滤条件。

        Returns:
            全部条件精确满足时返回 True。
        """
        logger.debug("精确匹配 Milvus metadata 入口", filter_count=len(filters or {}))
        for raw_key, raw_value in (filters or {}).items():
            is_not = raw_key.startswith("not:")
            key = raw_key[4:] if is_not else raw_key
            expected = raw_value
            if isinstance(raw_value, dict) and "$ne" in raw_value:
                is_not = True
                expected = raw_value["$ne"]
            matched = metadata.get(key) == expected
            if matched == is_not:
                logger.info("精确匹配 Milvus metadata 完成", matched=False, key=key)
                return False
        logger.info("精确匹配 Milvus metadata 完成", matched=True)
        return True

    # ── 检索 ──

    async def search(self, query: str, top_k: int = 5,
                     filters: dict[str, str] | None = None) -> list[VectorSearchResult]:
        """执行语义检索并对 metadata 候选做精确过滤。

        Args:
            query: 查询文本。
            top_k: 最大返回数。
            filters: metadata 精确过滤条件。

        Returns:
            匹配的向量检索结果。
        """
        logger.debug("Milvus search 入口", top_k=top_k, filter_count=len(filters or {}))
        _start = time.monotonic()
        emb = await self._embed(query)
        col = self._get_collection()
        candidate_limit = 50 if filters else min(top_k, 50)
        results = col.search(
            data=[emb], anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=candidate_limit, expr=self._to_expr(filters),
            output_fields=["id", "content", "metadata"])
        hits = []
        for hit in results[0]:
            e = hit.entity
            m = e.get("metadata") or "{}"
            metadata = json.loads(m) if isinstance(m, str) else (m or {})
            if not self._metadata_matches(metadata, filters):
                continue
            hits.append(VectorSearchResult(
                id=e.get("id") or "", content=e.get("content") or "",
                metadata=metadata,
                score=round(hit.score, 4)))
        result = hits[:min(top_k, 50)]
        logger.info("Milvus search 完成", hits=len(result),
                    elapsed_ms=round((time.monotonic()-_start)*1000))
        return result

    async def get_by_id(self, entry_id: str) -> VectorEntry | None:
        """按安全引用的 ID 精确查询记录。

        Args:
            entry_id: 向量记录 ID。

        Returns:
            匹配记录；不存在时返回 None。
        """
        logger.debug("Milvus ID 查询入口", entry_id=entry_id)
        col = self._get_collection()
        r = col.query(expr=f"id == {json.dumps(entry_id, ensure_ascii=False)}",
                      output_fields=["id", "content", "metadata"], limit=1)
        if not r:
            logger.info("Milvus ID 查询完成", entry_id=entry_id, found=False)
            return None
        m = r[0].get("metadata") or "{}"
        result = VectorEntry(id=r[0]["id"], content=r[0].get("content") or "",
                             metadata=json.loads(m) if isinstance(m, str) else (m or {}))
        logger.info("Milvus ID 查询完成", entry_id=entry_id, found=True)
        return result

    async def get_by_filter(self, filters: dict[str, str], limit: int = 100) -> list[VectorEntry]:
        """按 metadata 精确条件查询记录。

        Args:
            filters: metadata 过滤条件。
            limit: 最大返回数。

        Returns:
            精确匹配的记录列表。
        """
        logger.debug("Milvus metadata 查询入口", filter_count=len(filters), limit=limit)
        col = self._get_collection()
        rows = col.query(expr=self._to_expr(filters) or "",
                         output_fields=["id", "content", "metadata"], limit=100000)
        result: list[VectorEntry] = []
        for row in rows:
            raw_metadata = row.get("metadata") or "{}"
            metadata = (
                json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
            )
            if self._metadata_matches(metadata, filters):
                result.append(VectorEntry(
                    id=row["id"], content=row.get("content") or "", metadata=metadata,
                ))
                if len(result) >= limit:
                    break
        logger.info("Milvus metadata 查询完成", count=len(result))
        return result

    # ── 写入 ──

    async def upsert(self, entries: list[VectorEntry]) -> int:
        if not entries:
            return 0
        async with self._write_lock:
            col = self._get_collection()
            ids = [e.id for e in entries]
            embs = [e.embedding or await self._embed(e.content) for e in entries]
            existing = col.query(expr=f'id in {json.dumps(ids)}', output_fields=["id"])
            if existing:
                col.delete(expr=f'id in {json.dumps([r["id"] for r in existing])}')
            col.insert([ids, embs, [e.content for e in entries],
                        [json.dumps(e.metadata, ensure_ascii=False) for e in entries]])
            col.flush()
        logger.debug("Milvus upsert 完成", count=len(entries))
        return len(entries)

    async def delete_by_ids(self, ids: list[str]) -> int:
        if not ids:
            return 0
        async with self._write_lock:
            self._get_collection().delete(expr=f'id in {json.dumps(ids)}')
        return len(ids)

    async def delete_by_filter(self, filters: dict[str, str]) -> int:
        """仅删除 metadata 精确匹配的记录。

        Args:
            filters: metadata 过滤条件。

        Returns:
            实际删除的记录数。
        """
        logger.debug("Milvus metadata 删除入口", filter_count=len(filters))
        if not filters:
            logger.info("Milvus metadata 删除完成", count=0)
            return 0
        async with self._write_lock:
            col = self._get_collection()
            rows = col.query(
                expr=self._to_expr(filters) or "",
                output_fields=["id", "metadata"],
                limit=100000,
            )
            ids = []
            for row in rows:
                raw_metadata = row.get("metadata") or "{}"
                metadata = (
                    json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
                )
                if self._metadata_matches(metadata, filters):
                    ids.append(row["id"])
            if ids:
                col.delete(expr=f"id in {json.dumps(ids, ensure_ascii=False)}")
        logger.info("Milvus metadata 删除完成", count=len(ids))
        return len(ids)

    # ── 管理 ──

    async def count(self, filters: dict[str, str] | None = None) -> int:
        """统计全部记录或 metadata 精确匹配记录。

        Args:
            filters: 可选的 metadata 过滤条件。

        Returns:
            记录数量。
        """
        logger.debug("Milvus count 入口", filter_count=len(filters or {}))
        col = self._get_collection()
        if filters:
            rows = col.query(
                expr=self._to_expr(filters) or "",
                output_fields=["id", "metadata"],
                limit=100000,
            )
            result = 0
            for row in rows:
                raw_metadata = row.get("metadata") or "{}"
                metadata = (
                    json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
                )
                result += int(self._metadata_matches(metadata, filters))
        else:
            result = col.num_entities
        logger.info("Milvus count 完成", count=result)
        return result

    async def health_check(self) -> bool:
        logger.debug("MilvusVectorStore 健康检查入口")
        try:
            from pymilvus import utility
            result = utility.get_connection_addr("default") is not None
            logger.info("MilvusVectorStore 健康检查完成", healthy=result)
            return result
        except Exception as exc:
            logger.error(
                "MilvusVectorStore 健康检查失败",
                error=str(exc),
                exc_info=True,
            )
            return False
