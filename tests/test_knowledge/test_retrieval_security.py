"""知识检索租户/数据源过滤与引用测试，覆盖 KB-R1 和 KB-R7。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

logger = logging.getLogger(__name__)


class TestKnowledgeFilters:
    """覆盖检索过滤条件构造。"""

    # 验证多租户检索必带租户、可见性和数据源过滤。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_build_filters_requires_tenant_and_scope(self, monkeypatch):
        """构造过滤条件时不能只依赖向量相似度。"""
        logger.debug("test_build_filters_requires_tenant_and_scope 入口")
        import src.knowledge.retrieval as retrieval_module
        from src.app_context import AppContext, use_app_context

        monkeypatch.setattr(
            retrieval_module,
            "get_current_tenant_id",
            lambda: 9,
        )

        with use_app_context(AppContext(SimpleNamespace(multi_tenant=True))):
            filters = retrieval_module.build_knowledge_filters(
                datasource="orders",
                category="business_rule",
                asset_id="asset-1",
            )

        assert filters == {
            "tenant_id": 9,
            "visibility": "tenant",
            "datasource": "orders",
            "category": "business_rule",
            "asset_id": "asset-1",
        }
        logger.info("test_build_filters_requires_tenant_and_scope 完成")

    # 验证可访问知识由 system、当前租户 tenant、当前用户 private 三组过滤组成。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_build_accessible_filters_contains_three_scopes(self, monkeypatch):
        """三范围必须分别查询，private 过滤必须包含 owner_user_id。"""
        logger.debug("test_build_accessible_filters_contains_three_scopes 入口")
        import src.knowledge.retrieval as retrieval_module
        from src.app_context import AppContext, use_app_context

        monkeypatch.setattr(retrieval_module, "get_current_tenant_id", lambda: 9)
        monkeypatch.setattr(retrieval_module, "get_current_user_id", lambda: 4)

        with use_app_context(AppContext(SimpleNamespace(multi_tenant=True))):
            filters = retrieval_module.build_accessible_knowledge_filters(
                datasource="orders",
                category="business_rule",
            )

        assert filters == [
            {"visibility": "system", "datasource": "orders", "category": "business_rule"},
            {
                "visibility": "tenant", "tenant_id": 9,
                "datasource": "orders", "category": "business_rule",
            },
            {
                "visibility": "private", "tenant_id": 9, "owner_user_id": 4,
                "datasource": "orders", "category": "business_rule",
            },
        ]
        logger.info("test_build_accessible_filters_contains_three_scopes 完成")

    # 验证检索结果转换为带来源定位的 Evidence，而不是裸文本。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_returns_citations_and_forwards_filters(self, monkeypatch):
        """向量召回必须携带租户/数据源过滤并生成引用定位。"""
        logger.debug("test_search_returns_citations_and_forwards_filters 入口")
        from src.memory.vector_store import VectorSearchResult
        import src.knowledge.retrieval as retrieval_module

        monkeypatch.setattr(
            retrieval_module,
            "build_accessible_knowledge_filters",
            lambda **kwargs: [{"tenant_id": 9, "visibility": "tenant", "datasource": "orders"}],
        )
        store = SimpleNamespace(search=AsyncMock(return_value=[VectorSearchResult(
            id="doc-1",
            content="GMV 口径",
            metadata={
                "tenant_id": 9,
                "source_file": "metrics.md",
                "document_version": "v2",
                "locator": {"page": 2},
            },
            score=0.92,
        )]))

        results = await retrieval_module.search_knowledge(
            store, "GMV", datasource="orders", top_k=3,
        )

        assert results[0].source_id == "doc-1"
        assert results[0].version == "v2"
        assert results[0].locator == {"page": 2}
        store.search.assert_awaited_once_with(
            "GMV", top_k=3,
            filters={"tenant_id": 9, "visibility": "tenant", "datasource": "orders"},
        )
        logger.info("test_search_returns_citations_and_forwards_filters 完成")

    async def test_search_decodes_locator_json(self):
        """VectorStore 返回 locator_json 时应恢复为结构化定位。"""
        logger.debug("test_search_decodes_locator_json 入口")
        from src.memory.vector_store import VectorSearchResult
        import src.knowledge.retrieval as retrieval_module

        store = SimpleNamespace(search=AsyncMock(return_value=[VectorSearchResult(
            id="doc-json", content="报告段落", metadata={
                "document_version": "v3", "locator_json": '{"page": 9, "paragraph": 2}',
            }, score=0.8,
        )]))
        results = await retrieval_module.search_knowledge(store, "报告", top_k=1)
        assert results[0].locator == {"page": 9, "paragraph": 2}
        logger.info("test_search_decodes_locator_json 完成")


class TestUploadVectorStoreBoundary:
    """覆盖上传模块不绕过 VectorStore。"""

    # 验证上传分块通过抽象接口写入，兼容 Chroma/PG/Milvus。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_upload_writes_through_vector_store(self, monkeypatch):
        """_write_to_chromadb 应调用 VectorStore.upsert。"""
        logger.debug("test_upload_writes_through_vector_store 入口")
        from src.knowledge.doc_parser import DocChunk, ChunkConfig, ChunkStrategy
        import src.knowledge.upload_manager as upload_module

        store = SimpleNamespace(upsert=AsyncMock(return_value=1))
        monkeypatch.setattr(upload_module, "get_vector_store", AsyncMock(return_value=store))
        chunks = [DocChunk(
            id="chunk-1", content="指标内容", metadata={"strategy": "fixed", "chunk_size": 4},
        )]

        await upload_module._write_to_chromadb(
            chunks,
            "metrics.md",
            ChunkConfig(strategy=ChunkStrategy.FIXED),
            category="metric",
            tenant_id=2,
            user_id=8,
            knowledge_scope="private",
            tag_ids=[1, 2],
            tag_names=["数据字典", "PostgreSQL"],
            datasource="orders",
        )

        store.upsert.assert_awaited_once()
        entry = store.upsert.await_args.args[0][0]
        assert entry.id == "private:2:8:chunk-1"
        assert entry.metadata["tenant_id"] == 2
        assert entry.metadata["owner_user_id"] == 8
        assert entry.metadata["visibility"] == "private"
        assert entry.metadata["tag_ids_json"] == "[1, 2]"
        assert entry.metadata["tags"] == "数据字典,PostgreSQL"
        assert entry.metadata["datasource"] == "orders"
        assert "locator" not in entry.metadata
        assert entry.metadata["locator_json"] == "{}"
        logger.info("test_upload_writes_through_vector_store 完成")

    async def test_upload_serializes_locator_for_chroma_metadata(self, monkeypatch):
        """上传分块的页码和段落定位必须序列化为 Chroma 可接受的标量。"""
        logger.debug("test_upload_serializes_locator_for_chroma_metadata 入口")
        from src.knowledge.doc_parser import DocChunk, ChunkConfig, ChunkStrategy
        import src.knowledge.upload_manager as upload_module

        store = SimpleNamespace(upsert=AsyncMock(return_value=1))
        monkeypatch.setattr(upload_module, "get_vector_store", AsyncMock(return_value=store))
        chunks = [DocChunk(
            id="chunk-locator", content="正文", metadata={"page": 3, "paragraph": 5},
        )]

        await upload_module._write_to_chromadb(
            chunks, "report.pdf", ChunkConfig(strategy=ChunkStrategy.FIXED), tenant_id=2, user_id=8,
        )

        entry = store.upsert.await_args.args[0][0]
        assert entry.metadata["locator_json"] == '{"page": 3, "paragraph": 5}'
        logger.info("test_upload_serializes_locator_for_chroma_metadata 完成")
