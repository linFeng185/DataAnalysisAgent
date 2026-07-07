"""
业务规则存储 — 封装 DocLoader + ChromaDB，提供文档索引与向量检索。
"""

from __future__ import annotations

from src.knowledge.models import KnowledgeEntry, KnowledgeSource
from src.knowledge.doc_loader import DocLoader
from src.logging_config import get_logger

logger = get_logger(__name__)


class BusinessRuleStore:
    """
    业务规则存储。

    启动时扫描 docs/metrics/ 目录索引 Markdown 文档，
    运行时通过 ChromaDB 向量检索匹配业务规则。
    """

    def __init__(self, collection, docs_dir: str = "docs/metrics") -> None:
        self._collection = collection
        self._docs_dir = docs_dir
        self._initialized = False

    async def initialize(self) -> None:
        """扫描 docs/metrics/ → DocLoader → 幂等写入 ChromaDB。"""
        if self._initialized:
            return
        try:
            loader = DocLoader(self._docs_dir)
            entries = loader.scan_and_load()
            if entries:
                await self._upsert_rules(entries)
                logger.info("业务规则索引完成", count=len(entries))
            else:
                logger.info("未发现业务规则文档")
            self._initialized = True
        except Exception as e:
            logger.error("业务规则初始化失败", error=str(e))

    async def search_business_rules(
        self, query: str, top_k: int = 5
    ) -> list[KnowledgeEntry]:
        """
        向量检索匹配业务规则。

        按 category=business_rule 过滤，返回匹配的 KnowledgeEntry 列表。
        Phase 1 使用 ChromaDB 的 metadata 过滤做精确匹配，
        后续 Phase 可升级为语义向量检索。
        """
        try:
            from src.memory.vector_store import get_vector_store
            store = await get_vector_store()
            results = await store.get_by_filter({"category": "business_rule"}, limit=top_k)
            return [KnowledgeEntry.from_dict({"id": r.id, "content": r.content, **r.metadata})
                    for r in results]
        except Exception as e:
            logger.warning("业务规则检索失败", error=str(e))
            return []

    async def _upsert_rules(self, entries: list[KnowledgeEntry]) -> None:
        """幂等写入向量存储。"""
        if not entries:
            return
        try:
            from src.memory.vector_store import VectorEntry, get_vector_store
            store = await get_vector_store()
            await store.upsert([VectorEntry(id=e.id, content=e.content, metadata=e.to_dict())
                                for e in entries])
        except Exception as e:
            logger.error("业务规则写入失败", error=str(e))
