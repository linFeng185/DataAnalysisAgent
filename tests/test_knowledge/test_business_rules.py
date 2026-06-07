"""BusinessRuleStore test."""
from __future__ import annotations
import os, tempfile, asyncio
from unittest.mock import MagicMock
from src.knowledge.models import KnowledgeSource

class TestBusinessRuleStore:
    def test_initialize_indexes_docs(self):
        from src.knowledge.business_rules import BusinessRuleStore
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                md = os.path.join(tmp, "rule.md")
                with open(md, "w", encoding="utf-8") as f:
                    f.write("---" + chr(10) + "category: business_rule" + chr(10) + "tags: [demo]" + chr(10) + "---" + chr(10)*2 + "# R" + chr(10)*2 + "## Rule" + chr(10) + "Do X")
                mock_coll = MagicMock()
                store = BusinessRuleStore(mock_coll, docs_dir=tmp)
                await store.initialize()
                assert store._initialized
                assert mock_coll.add.called
        asyncio.run(go())

    def test_initialize_empty_directory(self):
        from src.knowledge.business_rules import BusinessRuleStore
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                mock_coll = MagicMock()
                store = BusinessRuleStore(mock_coll, docs_dir=tmp)
                await store.initialize()
                assert store._initialized
        asyncio.run(go())

    def test_search_returns_entries(self):
        from src.knowledge.business_rules import BusinessRuleStore
        async def go():
            mock_coll = MagicMock()
            mock_coll.get.return_value = {"ids": ["x"], "metadatas": [{"source": "manual_doc", "category": "business_rule", "table_name": "o", "column_name": "", "tags": ["x"], "created_at": "2026-01-01T00:00:00+00:00", "ttl": "0", "meta_json": "{}"}], "documents": ["text"]}
            store = BusinessRuleStore(mock_coll)
            results = await store.search_business_rules("q")
            assert len(results) >= 1
            assert results[0].source == KnowledgeSource.MANUAL_DOC
        asyncio.run(go())

    def test_search_empty(self):
        from src.knowledge.business_rules import BusinessRuleStore
        async def go():
            mock_coll = MagicMock()
            mock_coll.get.return_value = {"ids": [], "metadatas": [], "documents": []}
            store = BusinessRuleStore(mock_coll)
            results = await store.search_business_rules("nonexistent")
            assert results == []
        asyncio.run(go())
