"""ChromaDB 适配器 — 封装现有 chromadb.Collection，实现 VectorStore 接口。

兼容已有 ChromaDB 数据，不破坏现有行为。
所有写操作通过 asyncio.Lock 保护，防止 ChromaDB SQLite 后端并发冲突。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.memory.vector_store import VectorEntry, VectorSearchResult, VectorStore
from src.logging_config import get_logger

logger = get_logger(__name__)


class ChromaVectorStore(VectorStore):
    """ChromaDB 适配器，封装现有 chromadb.Collection。

    写入操作使用 asyncio.Lock 保护（ChromaDB SQLite 后端不支持并发写）。
    embedding 参数可选——若不传则使用 ChromaDB 内置的 embedding_function。
    """

    def __init__(self, collection, embedding_fn=None):
        """初始化适配器。

        Args:
            collection: chromadb.Collection 实例
            embedding_fn: 可选的嵌入函数 callable(text) → list[float]
        """
        self._col = collection
        self._embed = embedding_fn
        self._write_lock = asyncio.Lock()

    # ── 检索 ──

    async def search(self, query: str, top_k: int = 5,
                     filters: dict[str, str] | None = None) -> list[VectorSearchResult]:
        """语义向量搜索。

        Args:
            query: 搜索查询文本
            top_k: 返回结果数上限（内部限制 ≤50）
            filters: metadata 精确过滤条件

        Returns: VectorSearchResult 列表，按 score 降序
        """
        top_k = min(top_k, 50)
        where = _build_where(filters)
        if self._embed:
            emb = self._embed(query)
            raw = self._col.query(query_embeddings=[emb], n_results=top_k, where=where)
        else:
            raw = self._col.query(query_texts=[query], n_results=top_k, where=where)
        return _parse_results(raw)

    async def get_by_id(self, entry_id: str) -> VectorEntry | None:
        """按 ID 精确获取单条。

        Args:
            entry_id: ChromaDB 文档 ID

        Returns: 匹配的 VectorEntry，不存在返回 None
        """
        raw = self._col.get(ids=[entry_id])
        ids = raw.get("ids", [])
        if not ids:
            return None
        docs = raw.get("documents", [])
        metas = raw.get("metadatas", [])
        return VectorEntry(id=ids[0],
                           content=docs[0] if docs else "",
                           metadata=metas[0] if metas else {})

    async def get_by_filter(self, filters: dict[str, str],
                            limit: int = 100) -> list[VectorEntry]:
        """按 metadata 精确过滤，不涉及向量相似度计算。

        Args:
            filters: metadata 键值对
            limit: 返回条数上限

        Returns: 匹配的 VectorEntry 列表
        """
        raw = self._col.get(where=_build_where(filters))
        ids = raw.get("ids", [])
        docs = raw.get("documents", [])
        metas = raw.get("metadatas", [])
        return [VectorEntry(id=ids[i],
                            content=docs[i] if i < len(docs) else "",
                            metadata=metas[i] if i < len(metas) else {})
                for i in range(min(len(ids), limit))]

    # ── 写入 ──

    async def upsert(self, entries: list[VectorEntry]) -> int:
        """批量插入或更新。

        如果 embedding 已预计算则直接传入，否则由 ChromaDB 内置 embedding_fn 计算。
        写入步骤受 _write_lock 保护。

        Args:
            entries: 待写入的条目列表

        Returns: 实际写入数

        Raises:
            ValueError: embedding 数量与 entries 数量不匹配
        """
        if not entries:
            return 0
        ids, docs, metas, embs = [], [], [], []
        for e in entries:
            ids.append(e.id); docs.append(e.content); metas.append(e.metadata)
            if e.embedding:
                embs.append(e.embedding)
        if embs and len(embs) != len(ids):
            raise ValueError(f"embeddings ({len(embs)}) != entries ({len(ids)})")
        kwargs: dict[str, Any] = {"ids": ids, "documents": docs, "metadatas": metas}
        if embs:
            kwargs["embeddings"] = embs
        async with self._write_lock:
            self._col.upsert(**kwargs)
        logger.debug("ChromaDB upsert 完成", count=len(ids))
        return len(ids)

    async def delete_by_ids(self, ids: list[str]) -> int:
        """按 ID 列表批量删除。

        Args:
            ids: 待删除的文档 ID 列表

        Returns: 实际删除数
        """
        if not ids:
            return 0
        async with self._write_lock:
            self._col.delete(ids=ids)
        return len(ids)

    async def delete_by_filter(self, filters: dict[str, str]) -> int:
        """按 metadata 过滤批量删除。

        Args:
            filters: metadata 键值对

        Returns: 实际删除数（ChromaDB 不返回精确计数，返回 -1）
        """
        where = _build_where(filters)
        if not where:
            return 0
        async with self._write_lock:
            self._col.delete(where=where)
        return -1

    # ── 管理 ──

    async def count(self, filters: dict[str, str] | None = None) -> int:
        """获取条目总数，支持按 metadata 过滤。

        Args:
            filters: 可选的 metadata 过滤条件

        Returns: 条目数量
        """
        where = _build_where(filters)
        return len((self._col.get(where=where) if where else self._col.get()).get("ids", []))

    async def health_check(self) -> bool:
        """连通性检查——调用 count() 验证 Collection 可用。

        Returns: True 表示可用
        """
        try:
            self._col.count()
            return True
        except Exception:
            return False


def _build_where(filters: dict[str, str] | None) -> dict | None:
    """将 filters 字典转为 ChromaDB where 条件。

    支持 not: 前缀做不等匹配：{"not:status": "deleted"} → {"status": {"$ne": "deleted"}}

    Args:
        filters: 过滤条件 {"key": "val", "not:key": "excluded"}

    Returns: ChromaDB where 子句，无过滤返回 None
    """
    if not filters:
        return None
    result: dict[str, Any] = {}
    for k, v in filters.items():
        result[k[4:]] = {"$ne": v} if k.startswith("not:") else v
    return result or None


def _parse_results(raw: dict) -> list[VectorSearchResult]:
    """解析 ChromaDB query() 返回的原始 dict 为 VectorSearchResult 列表。

    将 ChromaDB 距离（越小越相似）转为 score（越大越相似，0~1）。

    Args:
        raw: ChromaDB query() 返回的原始 dict

    Returns: VectorSearchResult 列表
    """
    ids = raw.get("ids", [[]])[0]
    docs = raw.get("documents", [[]])[0]
    metas = raw.get("metadatas", [[]])[0]
    dists = raw.get("distances", [[]])[0]
    return [VectorSearchResult(
        id=ids[i],
        content=docs[i] if i < len(docs) else "",
        metadata=metas[i] if i < len(metas) else {},
        score=round(1.0 - min(dists[i] if i < len(dists) else 1.0, 1.0), 4),
    ) for i in range(len(ids))]
