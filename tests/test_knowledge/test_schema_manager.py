"""SchemaManager 测试 -- 缓存/内省/组装/格式化。"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.knowledge.models import AUTO_TTL_SECONDS, KnowledgeEntry, KnowledgeSource


class TestFormatHelpers:
    """_format_table_summary + _format_column_detail 测试。"""

    def test_table_summary_full(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        result = m._format_table_summary("orders", "订单表", "id(UInt64), amount(Float64)")
        assert "orders" in result and "订单表" in result and "id(UInt64)" in result

    def test_table_summary_no_desc(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        result = m._format_table_summary("t", "", "id(UInt64)")
        assert " -- " not in result

    def test_column_detail_with_comment(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        result = m._format_column_detail("o", "amt", "Float64", "comment")
        assert result == "o.amt: Float64 - comment"

    def test_column_detail_no_comment(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        assert m._format_column_detail("o", "id", "UInt64", "") == "o.id: UInt64"


class TestEntryMatching:
    """_belongs_to_datasource 测试。"""

    def test_match(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        assert m._belongs_to_datasource("table:demo.orders", "demo")

    def test_no_match(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        assert not m._belongs_to_datasource("table:other.orders", "demo")

    def test_bad_format(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        assert not m._belongs_to_datasource("bad", "demo")


class TestExpiryCheck:
    """_any_expired 测试。"""

    def test_none_expired(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        entries = [
            KnowledgeEntry("a", "x", KnowledgeSource.MANUAL_DOC, "table", ttl=0),
            KnowledgeEntry("b", "y", KnowledgeSource.MANUAL_DOC, "table", ttl=0),
        ]
        assert not m._any_expired(entries)

    def test_one_expired(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        entries = [
            KnowledgeEntry("a", "x", KnowledgeSource.AUTO_INTROSPECT, "table", ttl=1,
                created_at=datetime(2000, 1, 1, tzinfo=timezone.utc)),
            KnowledgeEntry("b", "y", KnowledgeSource.MANUAL_DOC, "table", ttl=0),
        ]
        assert m._any_expired(entries)


class TestMergeEntries:
    """_merge_entries 合并优先级测试。"""

    def test_doc_overrides_auto(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        auto = [KnowledgeEntry("t:a", "auto", KnowledgeSource.AUTO_INTROSPECT, "table", table_name="a")]
        doc = [KnowledgeEntry("t:a", "manual", KnowledgeSource.MANUAL_DOC, "table", table_name="a")]
        merged = m._merge_entries(doc, auto)
        assert merged[0].content == "manual"

    def test_new_doc_added(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        auto = [KnowledgeEntry("t:a", "a", KnowledgeSource.AUTO_INTROSPECT, "table", table_name="a")]
        doc = [KnowledgeEntry("t:b", "b", KnowledgeSource.MANUAL_DOC, "table", table_name="b")]
        merged = m._merge_entries(doc, auto)
        assert len(merged) == 2


class TestBuildSnapshot:
    """_build_snapshot 组装测试。"""

    def test_empty(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        snapshot = m._build_snapshot([])
        assert snapshot.tables == []

    def test_single_table_with_column(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        entries = [
            KnowledgeEntry("table:x.o", "o", KnowledgeSource.AUTO_INTROSPECT, "table",
                table_name="o", metadata={"row_count_estimate": 100}),
            KnowledgeEntry("column:x.o.id", "o.id: Int", KnowledgeSource.AUTO_INTROSPECT, "column",
                table_name="o", column_name="id", metadata={"type": "Int", "is_primary_key": True}),
        ]
        snapshot = m._build_snapshot(entries)
        assert len(snapshot.tables) == 1
        assert snapshot.tables[0].name == "o"
        assert len(snapshot.tables[0].columns) == 1
        assert snapshot.tables[0].columns[0].is_primary_key is True


class TestLoadFromDocs:
    """_load_from_docs Phase 1 桩。"""

    def test_returns_empty(self):
        from src.knowledge.schema_manager import SchemaManager
        m = SchemaManager()
        assert m._load_from_docs("any") == []


class TestSnapshotToEntries:
    """_snapshot_to_entries 转换测试。"""

    def test_converts_tables_and_columns(self):
        from src.knowledge.schema_manager import SchemaManager
        from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema

        m = SchemaManager()
        snapshot = SchemaSnapshot(tables=[
            TableSchema(name="orders", description="订单表",
                columns=[ColumnInfo(name="id", type="UInt64", is_primary_key=True, is_nullable=False)],
                row_count_estimate=100),
        ])
        entries = m._snapshot_to_entries("demo", snapshot)
        assert len(entries) == 2  # 1 table + 1 column
        table_e = [e for e in entries if e.category == "table"][0]
        assert table_e.id == "table:demo.orders"
        assert table_e.ttl == AUTO_TTL_SECONDS
        col_e = [e for e in entries if e.category == "column"][0]
        assert col_e.metadata["is_primary_key"] is True


class TestSchemaManagerIntegration:
    """SchemaManager ChromaDB 集成测试 -- mock collection 避免 ChromaDB 初始化。"""

    def test_cache_write_and_read(self):
        from src.knowledge.schema_manager import SchemaManager
        from unittest.mock import MagicMock

        m = SchemaManager()
        m._collection = MagicMock()
        m._collection.get.return_value = {"ids": [], "metadatas": [], "documents": []}
        m._initialized = True

        entries = [KnowledgeEntry("table:x.o", "desc", KnowledgeSource.AUTO_INTROSPECT,
            "table", table_name="o", ttl=AUTO_TTL_SECONDS)]
        m._upsert_to_cache(entries)
        # 验证 add() 被调用
        assert m._collection.add.called

    def test_query_cache_empty(self):
        from src.knowledge.schema_manager import SchemaManager
        from unittest.mock import MagicMock

        m = SchemaManager()
        m._collection = MagicMock()
        m._collection.get.return_value = {"ids": [], "metadatas": [], "documents": []}
        m._initialized = True

        cached = m._query_cache("nonexistent")
        assert cached == []

    def test_query_cache_finds_entries(self):
        from src.knowledge.schema_manager import SchemaManager
        from unittest.mock import MagicMock
        from datetime import datetime, timezone

        m = SchemaManager()
        now = datetime.now(timezone.utc).isoformat()
        m._collection = MagicMock()
        m._collection.get.return_value = {
            "ids": ["table:demo.orders"],
            "metadatas": [{
                "source": "auto_introspect", "category": "table",
                "table_name": "orders", "column_name": "", "tags": "",
                "created_at": now, "ttl": str(AUTO_TTL_SECONDS), "meta_json": "{}"
            }],
            "documents": ["orders - 订单表"],
        }
        m._initialized = True

        cached = m._query_cache("demo")
        assert len(cached) == 1
        assert cached[0].id == "table:demo.orders"
        assert cached[0].table_name == "orders"

    def test_singleton(self):
        from src.knowledge.schema_manager import get_schema_manager
        m1 = get_schema_manager()
        m2 = get_schema_manager()
        assert m1 is m2
