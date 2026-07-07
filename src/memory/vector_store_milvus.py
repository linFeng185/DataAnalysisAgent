"""MilvusVectorStore — Milvus 实现的 VectorStore。

前置条件: Docker: docker run -d --name milvus -p 19530:19530 milvusdb/milvus:latest
          pip install pymilvus
启用: config.py 中 vector_store_type = "milvus"
"""

from __future__ import annotations

import asyncio, json, time

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

    def _to_expr(self, filters: dict[str, str] | None) -> str | None:
        if not filters:
            return None
        parts = [f'metadata like "%\"{k}\":\"{v}\"%"' for k, v in filters.items() if not k.startswith("not:")]
        return " && ".join(parts) if parts else None

    # ── 检索 ──

    async def search(self, query: str, top_k: int = 5,
                     filters: dict[str, str] | None = None) -> list[VectorSearchResult]:
        _start = time.monotonic()
        emb = await self._embed(query)
        col = self._get_collection()
        results = col.search(
            data=[emb], anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=min(top_k, 50), expr=self._to_expr(filters),
            output_fields=["id", "content", "metadata"])
        hits = []
        for hit in results[0]:
            e = hit.entity; m = e.get("metadata") or "{}"
            hits.append(VectorSearchResult(
                id=e.get("id") or "", content=e.get("content") or "",
                metadata=json.loads(m) if isinstance(m, str) else (m or {}),
                score=round(hit.score, 4)))
        logger.debug("Milvus search", hits=len(hits),
                     elapsed_ms=round((time.monotonic()-_start)*1000))
        return hits

    async def get_by_id(self, entry_id: str) -> VectorEntry | None:
        col = self._get_collection()
        r = col.query(expr=f'id == "{entry_id}"',
                      output_fields=["id", "content", "metadata"], limit=1)
        if not r: return None
        m = r[0].get("metadata") or "{}"
        return VectorEntry(id=r[0]["id"], content=r[0].get("content") or "",
                           metadata=json.loads(m) if isinstance(m, str) else (m or {}))

    async def get_by_filter(self, filters: dict[str, str], limit: int = 100) -> list[VectorEntry]:
        col = self._get_collection()
        rows = col.query(expr=self._to_expr(filters) or "",
                         output_fields=["id", "content", "metadata"], limit=limit)
        return [VectorEntry(id=r["id"], content=r.get("content") or "",
                            metadata=json.loads(r.get("metadata") or "{}") if isinstance(r.get("metadata"), str) else (r.get("metadata") or {}))
                for r in rows]

    # ── 写入 ──

    async def upsert(self, entries: list[VectorEntry]) -> int:
        if not entries: return 0
        async with self._write_lock:
            col = self._get_collection()
            ids = [e.id for e in entries]
            embs = [e.embedding or await self._embed(e.content) for e in entries]
            existing = col.query(expr=f'id in {json.dumps(ids)}', output_fields=["id"])
            if existing: col.delete(expr=f'id in {json.dumps([r["id"] for r in existing])}')
            col.insert([ids, embs, [e.content for e in entries],
                        [json.dumps(e.metadata, ensure_ascii=False) for e in entries]])
            col.flush()
        logger.debug("Milvus upsert 完成", count=len(entries))
        return len(entries)

    async def delete_by_ids(self, ids: list[str]) -> int:
        if not ids: return 0
        async with self._write_lock:
            self._get_collection().delete(expr=f'id in {json.dumps(ids)}')
        return len(ids)

    async def delete_by_filter(self, filters: dict[str, str]) -> int:
        e = self._to_expr(filters)
        if not e: return 0
        async with self._write_lock:
            self._get_collection().delete(expr=e)
        return -1

    # ── 管理 ──

    async def count(self, filters: dict[str, str] | None = None) -> int:
        col = self._get_collection()
        if filters:
            return len(col.query(expr=self._to_expr(filters) or "", output_fields=["id"], limit=100000))
        return col.num_entities

    async def health_check(self) -> bool:
        try:
            from pymilvus import utility
            return utility.get_connection_addr("default") is not None
        except Exception:
            return False
