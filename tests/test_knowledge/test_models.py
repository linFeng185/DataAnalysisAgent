"""KnowledgeSource + KnowledgeEntry 数据模型测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.knowledge.models import (
    AUTO_TTL_SECONDS,
    KnowledgeEntry,
    KnowledgeSource,
    source_priority,
)


class TestKnowledgeSource:
    """6.2.1 KnowledgeSource 枚举测试。"""

    def test_all_sources(self):
        sources = list(KnowledgeSource)
        assert len(sources) == 6
        assert KnowledgeSource.MANUAL_DOC in sources

    def test_source_values(self):
        assert KnowledgeSource.MANUAL_DOC.value == "manual_doc"
        assert KnowledgeSource.AUTO_INTROSPECT.value == "auto_introspect"

    def test_str_enum_coercion(self):
        assert KnowledgeSource("auto_introspect") == KnowledgeSource.AUTO_INTROSPECT


class TestSourcePriority:
    """source_priority() 测试。"""

    def test_manual_higher_than_auto(self):
        assert source_priority(KnowledgeSource.MANUAL_DOC) < source_priority(KnowledgeSource.AUTO_INTROSPECT)

    def test_warning_lowest(self):
        assert source_priority(KnowledgeSource.SYSTEM_WARNING) == 99

    def test_unknown_default_50(self):
        assert source_priority("unknown") == 50


class TestKnowledgeEntry:
    """6.2.2 KnowledgeEntry dataclass 测试。"""

    def test_create_basic(self):
        e = KnowledgeEntry(
            id="table:demo.orders", content="orders 订单表",
            source=KnowledgeSource.AUTO_INTROSPECT, category="table", table_name="orders",
        )
        assert e.id == "table:demo.orders"
        assert e.source == KnowledgeSource.AUTO_INTROSPECT

    def test_defaults(self):
        e = KnowledgeEntry(id="t", content="c", source=KnowledgeSource.DB_COMMENT, category="table")
        assert e.table_name == ""
        assert e.column_name == ""
        assert e.tags == []
        assert e.ttl == 0
        assert e.metadata == {}

    def test_is_expired_zero_ttl(self):
        e = KnowledgeEntry(id="t", content="c", source=KnowledgeSource.MANUAL_DOC, category="table", ttl=0)
        assert not e.is_expired()

    def test_is_expired_past(self):
        e = KnowledgeEntry(id="t", content="c", source=KnowledgeSource.AUTO_INTROSPECT,
            category="table", ttl=1,
            created_at=datetime.now(timezone.utc) - timedelta(seconds=2))
        assert e.is_expired()

    def test_is_expired_not_yet(self):
        e = KnowledgeEntry(id="t", content="c", source=KnowledgeSource.AUTO_INTROSPECT,
            category="table", ttl=999999)
        assert not e.is_expired()

    def test_auto_ttl_7_days(self):
        assert AUTO_TTL_SECONDS == 7 * 24 * 3600

    def test_to_dict(self):
        now = datetime.now(timezone.utc)
        e = KnowledgeEntry(id="table:x", content="desc", source=KnowledgeSource.DB_COMMENT,
            category="table", table_name="x", tags=["a"], created_at=now, ttl=3600,
            metadata={"rows": 100})
        d = e.to_dict()
        assert d["id"] == "table:x"
        assert d["source"] == "db_comment"
        assert d["tags"] == ["a"]
        assert "rows" in d["meta_json"]

    def test_from_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        original = KnowledgeEntry(id="col:demo.o.amt", content="o.amt: Float64",
            source=KnowledgeSource.AUTO_INTROSPECT, category="column",
            table_name="o", column_name="amt", created_at=now, ttl=AUTO_TTL_SECONDS,
            metadata={"type": "Float64"})
        d = original.to_dict()
        restored = KnowledgeEntry.from_dict(d)
        assert restored.id == original.id
        assert restored.source == original.source
        assert restored.column_name == "amt"
        assert restored.ttl == original.ttl

    def test_from_dict_minimal(self):
        d = {"id": "t", "content": "hi", "source": "manual_doc", "category": "table"}
        e = KnowledgeEntry.from_dict(d)
        assert e.id == "t"
        assert e.source == KnowledgeSource.MANUAL_DOC

    def test_from_dict_unknown_source_fallback(self):
        d = {"id": "t", "content": "hi", "source": "bogus", "category": "table"}
        e = KnowledgeEntry.from_dict(d)
        assert e.source == KnowledgeSource.AUTO_INTROSPECT
