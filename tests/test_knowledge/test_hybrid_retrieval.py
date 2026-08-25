"""知识库混合召回与不可信内容隔离测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestHybridRetrieval:
    """覆盖向量、关键词和字段精确命中的融合排序。"""

    async def test_hybrid_search_promotes_exact_field_match(self, monkeypatch):
        """向量相似度较低但字段名精确匹配的条目应被召回。"""
        # Arrange
        import src.knowledge.retrieval as retrieval_module
        from src.memory.vector_store import VectorEntry, VectorSearchResult

        monkeypatch.setattr(
            retrieval_module,
            "build_accessible_knowledge_filters",
            lambda **kwargs: [{"tenant_id": 1, "visibility": "tenant", "datasource": "demo"}],
        )
        store = SimpleNamespace(
            search=AsyncMock(return_value=[VectorSearchResult(
                id="semantic", content="订单金额业务口径", metadata={"table_name": "orders"}, score=0.7,
            )]),
            get_by_filter=AsyncMock(return_value=[VectorEntry(
                id="exact", content="amount 字段为订单金额，单位元",
                metadata={"table_name": "orders", "column_name": "amount"},
            )]),
        )

        # Act
        results = await retrieval_module.search_knowledge(
            store, "orders.amount", datasource="demo", top_k=2,
        )

        # Assert
        assert {item.source_id for item in results} == {"semantic", "exact"}
        assert results[0].source_id == "exact"
        assert results[0].scores["lexical"] > 0
        store.get_by_filter.assert_awaited_once()

    async def test_hybrid_search_keeps_acl_filters_on_keyword_branch(self, monkeypatch):
        """关键词召回也必须复用同一租户和可见性过滤。"""
        # Arrange
        import src.knowledge.retrieval as retrieval_module
        from src.memory.vector_store import VectorEntry

        filters = {"tenant_id": 8, "visibility": "tenant"}
        monkeypatch.setattr(
            retrieval_module,
            "build_accessible_knowledge_filters",
            lambda **kwargs: [filters],
        )
        store = SimpleNamespace(
            search=AsyncMock(return_value=[]),
            get_by_filter=AsyncMock(return_value=[VectorEntry(
                id="safe", content="仅租户 8 的资料", metadata={"tenant_id": 8},
            )]),
        )

        # Act
        await retrieval_module.search_knowledge(store, "资料", top_k=3)

        # Assert
        store.search.assert_awaited_once_with("资料", top_k=3, filters=filters)
        store.get_by_filter.assert_awaited_once_with(filters, limit=60)

    async def test_rerank_runs_off_event_loop(self, monkeypatch):
        """重排应通过线程池执行，避免同步计算阻塞 async 检索。"""
        import src.knowledge.retrieval as retrieval_module
        from src.memory.vector_store import VectorSearchResult

        monkeypatch.setattr(
            retrieval_module,
            "build_accessible_knowledge_filters",
            lambda **kwargs: [{"visibility": "tenant"}],
        )
        store = SimpleNamespace(
            search=AsyncMock(return_value=[VectorSearchResult(
                id="doc", content="正文", metadata={}, score=0.8,
            )]),
        )
        original_to_thread = retrieval_module.asyncio.to_thread
        to_thread = AsyncMock(side_effect=lambda func, *args: func(*args))
        monkeypatch.setattr(retrieval_module.asyncio, "to_thread", to_thread)

        result = await retrieval_module.search_knowledge(store, "正文", top_k=1, hybrid=False)

        assert result[0].source_id == "doc"
        to_thread.assert_awaited_once()
        monkeypatch.setattr(retrieval_module.asyncio, "to_thread", original_to_thread)

    async def test_business_rule_priority_beats_schema_enumerations(self, monkeypatch):
        """同一问题下人工业务规则应优先于自动内省列条目。"""
        # Arrange
        import src.knowledge.retrieval as retrieval_module
        from src.memory.vector_store import VectorEntry, VectorSearchResult

        monkeypatch.setattr(
            retrieval_module,
            "build_accessible_knowledge_filters",
            lambda **kwargs: [{"tenant_id": 1, "visibility": "tenant"} | (
                {"source": kwargs["source"]} if kwargs.get("source") else {}
            )],
        )

        class Store:
            async def search(self, query, top_k, filters):
                return [
                    VectorSearchResult(
                        id=f"enum-{index}",
                        content=f"users.vip_level: {index}普通",
                        metadata={
                            "category": "column",
                            "source": "auto_introspect",
                            "table_name": "users",
                        },
                        score=0.85 - index * 0.01,
                    )
                    for index in range(5)
                ]

            async def get_by_filter(self, filters, limit):
                if filters.get("source") in {None, "", "user_upload"}:
                    return [VectorEntry(
                        id="rule-1",
                        content="VIP 客户统计必须使用 customers.customer_level='VIP'",
                        metadata={
                            "category": "business_rule",
                            "source": "user_upload",
                            "source_file": "vip.md",
                        },
                    )]
                return []

        # Act
        results = await retrieval_module.search_knowledge(
            Store(), "VIP 客户统计口径", top_k=3,
        )

        # Assert
        assert results[0].source_id == "rule-1"
        assert results[0].metadata["category"] == "business_rule"
        assert results[0].scores["knowledge_priority"] > 0.9
