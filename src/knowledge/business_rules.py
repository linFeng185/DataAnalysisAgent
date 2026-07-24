"""
业务规则存储 — 封装 DocLoader + ChromaDB，提供文档索引与向量检索。
"""

from __future__ import annotations

from src.knowledge.models import KnowledgeEntry
from src.knowledge.doc_loader import DocLoader
from src.logging_config import get_logger
from src.memory.vector_store import VectorStore

logger = get_logger(__name__)


class BusinessRuleStore:
    """
    业务规则存储。

    启动时扫描 docs/metrics/ 目录索引 Markdown 文档，
    运行时通过 ChromaDB 向量检索匹配业务规则。
    """

    def __init__(self, store: VectorStore, docs_dir: str = "docs/metrics") -> None:
        """初始化只依赖 VectorStore 公共接口的业务规则存储。

        Args:
            store: 当前 AppContext 的 VectorStore 实例。
            docs_dir: 业务规则文档目录。

        Returns:
            无返回值。
        """
        logger.debug("业务规则存储初始化入口", docs_dir=docs_dir)
        self._store = store
        self._docs_dir = docs_dir
        self._initialized = False
        logger.info("业务规则存储初始化完成", docs_dir=docs_dir)

    async def initialize(self) -> None:
        """扫描 docs/metrics/ → DocLoader → 幂等写入 ChromaDB。"""
        logger.debug("业务规则初始化入口", initialized=self._initialized)
        if self._initialized:
            logger.info("业务规则初始化完成", reused=True)
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
        except Exception as exc:
            logger.error("业务规则初始化失败", error=str(exc), exc_info=True)
            return
        logger.info("业务规则初始化完成", reused=False)

    async def search_business_rules(
        self, query: str, top_k: int = 5
    ) -> list[KnowledgeEntry]:
        """
        向量检索匹配业务规则。

        按 category=business_rule 过滤，返回匹配的 KnowledgeEntry 列表。
        Phase 1 使用 ChromaDB 的 metadata 过滤做精确匹配，
        后续 Phase 可升级为语义向量检索。
        """
        logger.debug("业务规则检索入口", top_k=top_k, query_chars=len(query))
        try:
            filters = {"category": "business_rule"}
            from src.app_context import get_tenant_policy
            if get_tenant_policy().knowledge_isolation_enabled:
                from src.api.auth import get_current_tenant_id
                filters["tenant_id"] = get_current_tenant_id()
            results = await self._store.get_by_filter(filters, limit=top_k)
            entries = [
                KnowledgeEntry.from_dict({"id": r.id, "content": r.content, **r.metadata})
                for r in results
            ]
            logger.info("业务规则检索完成", count=len(entries))
            return entries
        except Exception as exc:
            logger.error("业务规则检索失败", error=str(exc), exc_info=True)
            return []

    async def _upsert_rules(self, entries: list[KnowledgeEntry]) -> None:
        """幂等写入向量存储。"""
        logger.debug("业务规则写入入口", count=len(entries))
        if not entries:
            logger.info("业务规则写入完成", count=0)
            return
        try:
            from src.memory.vector_store import VectorEntry
            written = await self._store.upsert([
                VectorEntry(id=e.id, content=e.content, metadata=e.to_dict())
                for e in entries
            ])
        except Exception as exc:
            logger.error("业务规则写入失败", error=str(exc), exc_info=True)
            return
        logger.info("业务规则写入完成", count=written)
