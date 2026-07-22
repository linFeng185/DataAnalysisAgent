"""SchemaManager 测试 -- 缓存/内省/组装/格式化。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.knowledge.models import AUTO_TTL_SECONDS, KnowledgeEntry, KnowledgeSource


logger = logging.getLogger(__name__)


class TestLocalEmbeddingModel:
    """覆盖功能 6.1.1：配置的本地 ONNX 模型目录必须被实际使用。"""

    # 方法作用：验证本地嵌入函数实例指向配置目录而非 Chroma 默认缓存。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_create_embedding_function_uses_configured_directory(
        self, tmp_path, monkeypatch,
    ) -> None:
        """合法本地模型目录不得触发 HuggingFace 默认下载路径。"""
        logger.debug("test_create_embedding_function_uses_configured_directory 入口")
        try:
            # Arrange
            from chromadb.utils import embedding_functions
            from src.knowledge.schema_manager import SchemaManager

            for name in (
                "config.json", "model.onnx", "special_tokens_map.json",
                "tokenizer_config.json", "tokenizer.json", "vocab.txt",
            ):
                (tmp_path / name).write_text("test", encoding="utf-8")

            class FakeONNX:
                DOWNLOAD_PATH = "default-cache"
                EXTRACTED_FOLDER_NAME = "onnx"

                def __init__(self, preferred_providers=None):
                    self.preferred_providers = preferred_providers

            monkeypatch.setattr(embedding_functions, "ONNXMiniLM_L6_V2", FakeONNX)
            settings = SimpleNamespace(embedding_model_path=str(tmp_path))

            # Act
            result = SchemaManager._create_embedding_function(settings)

            # Assert
            assert result.DOWNLOAD_PATH == str(tmp_path)
            assert result.EXTRACTED_FOLDER_NAME == ""
            logger.info("test_create_embedding_function_uses_configured_directory 完成")
        except Exception as exc:
            logger.error(
                "test_create_embedding_function_uses_configured_directory 异常: %s",
                exc,
                exc_info=True,
            )
            raise


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
                table_name="o", column_name="id",
                metadata={"type": "Int", "comment": "主键", "is_primary_key": True}),
        ]
        snapshot = m._build_snapshot(entries)
        assert len(snapshot.tables) == 1
        assert snapshot.tables[0].name == "o"
        assert len(snapshot.tables[0].columns) == 1
        assert snapshot.tables[0].columns[0].is_primary_key is True
        assert snapshot.tables[0].columns[0].comment == "主键"


class TestSchemaCacheRecovery:
    """覆盖不完整 Schema 缓存触发重新内省。"""

    async def test_incomplete_table_cache_triggers_introspection(self, monkeypatch):
        """缓存只有表级条目时，应重新加载字段结构。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.knowledge.schema_manager import SchemaManager

        # 隔离连接级持久化缓存，确保本用例只验证旧向量缓存不完整的恢复路径。
        shared_cache = SimpleNamespace(
            get=AsyncMock(return_value=None),
            set=AsyncMock(),
            delete=AsyncMock(return_value=False),
        )
        manager = SchemaManager(datasource_cache=shared_cache)
        monkeypatch.setattr(manager, "_resolve_datasource", AsyncMock(return_value=None))
        table = KnowledgeEntry(
            "table:oracle_xe.customers", "customers", KnowledgeSource.AUTO_INTROSPECT,
            "table", table_name="customers",
        )
        column = KnowledgeEntry(
            "column:oracle_xe.customers.id", "customers.id: NUMBER",
            KnowledgeSource.AUTO_INTROSPECT, "column", table_name="customers",
            column_name="id", metadata={"type": "NUMBER", "comment": "主键"},
        )
        monkeypatch.setattr(manager, "_ensure_initialized", lambda: None)
        monkeypatch.setattr(manager, "_query_cache", AsyncMock(return_value=[table]))
        monkeypatch.setattr(manager, "_load_from_docs", lambda _: [])
        introspect = AsyncMock(return_value=[table, column])
        monkeypatch.setattr(manager, "_introspect_from_db", introspect)
        monkeypatch.setattr(manager, "_upsert_to_cache", AsyncMock())

        snapshot = await manager.get_or_fetch_schema("oracle_xe")

        assert snapshot.tables[0].columns[0].name == "id"
        introspect.assert_awaited_once_with("oracle_xe")

    # 验证自动内省刷新后会删除已经不在当前 Schema 中的孤儿缓存。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_expired_cache_refresh_deletes_stale_entries(self, monkeypatch):
        """过期缓存触发内省后，应删除新 Schema 中已不存在的旧字段。"""
        logger.debug("test_expired_cache_refresh_deletes_stale_entries 入口")
        try:
            # Arrange：旧缓存包含已删除字段，新内省只返回当前表和字段。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.memory.vector_store as vector_module
            from src.knowledge.schema_manager import SchemaManager

            expired_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            stale = KnowledgeEntry(
                "column:mysql_test.orders.legacy_id",
                "orders.legacy_id: BIGINT",
                KnowledgeSource.AUTO_INTROSPECT,
                "column",
                table_name="orders",
                column_name="legacy_id",
                created_at=expired_at,
                ttl=1,
                metadata={"datasource": "mysql_test", "type": "BIGINT"},
            )
            current_table = KnowledgeEntry(
                "table:mysql_test.orders",
                "orders",
                KnowledgeSource.AUTO_INTROSPECT,
                "table",
                table_name="orders",
                metadata={"datasource": "mysql_test"},
            )
            current_column = KnowledgeEntry(
                "column:mysql_test.orders.id",
                "orders.id: BIGINT",
                KnowledgeSource.AUTO_INTROSPECT,
                "column",
                table_name="orders",
                column_name="id",
                metadata={"datasource": "mysql_test", "type": "BIGINT"},
            )
            shared_cache = SimpleNamespace(
                get=AsyncMock(return_value=None),
                set=AsyncMock(),
                delete=AsyncMock(return_value=False),
            )
            manager = SchemaManager(datasource_cache=shared_cache)
            monkeypatch.setattr(manager, "_resolve_datasource", AsyncMock(return_value=None))
            monkeypatch.setattr(manager, "_ensure_initialized", lambda: None)
            monkeypatch.setattr(
                manager,
                "_query_cache",
                AsyncMock(return_value=[current_table, current_column, stale]),
            )
            monkeypatch.setattr(manager, "_load_from_docs", lambda _: [])
            monkeypatch.setattr(
                manager,
                "_introspect_from_db",
                AsyncMock(return_value=[current_table, current_column]),
            )
            monkeypatch.setattr(manager, "_upsert_to_cache", AsyncMock())
            store = type("Store", (), {"delete_by_ids": AsyncMock(return_value=1)})()
            monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=store))

            # Act：执行正常的缓存获取路径。
            snapshot = await manager.get_or_fetch_schema("mysql_test")

            # Assert：当前条目被保留，仅删除孤儿字段。
            assert [table.name for table in snapshot.tables] == ["orders"]
            store.delete_by_ids.assert_awaited_once_with([stale.id])
            logger.info(
                "test_expired_cache_refresh_deletes_stale_entries 完成",
                extra={"deleted_id": stale.id},
            )
        except Exception as exc:
            logger.error(
                "test_expired_cache_refresh_deletes_stale_entries 异常: %s",
                exc,
                exc_info=True,
            )
            raise


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

    async def test_cache_write_and_read(self, monkeypatch):
        from src.knowledge.schema_manager import SchemaManager
        from unittest.mock import AsyncMock
        import src.memory.vector_store as vector_module

        m = SchemaManager()
        m._initialized = True
        store = type("Store", (), {"upsert": AsyncMock(return_value=1)})()
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=store))

        entries = [KnowledgeEntry("table:x.o", "desc", KnowledgeSource.AUTO_INTROSPECT,
            "table", table_name="o", ttl=AUTO_TTL_SECONDS,
            metadata={"datasource": "x"})]
        await m._upsert_to_cache(entries)
        store.upsert.assert_awaited_once()
        saved = store.upsert.await_args.args[0][0]
        assert saved.metadata["datasource"] == "x"

    async def test_query_cache_empty(self, monkeypatch):
        from src.knowledge.schema_manager import SchemaManager
        from unittest.mock import AsyncMock
        import src.memory.vector_store as vector_module

        m = SchemaManager()
        m._initialized = True
        store = type("Store", (), {"get_by_filter": AsyncMock(return_value=[])})()
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=store))

        cached = await m._query_cache("nonexistent")
        assert cached == []

    async def test_query_cache_finds_entries(self, monkeypatch):
        from src.knowledge.schema_manager import SchemaManager
        from src.memory.vector_store import VectorEntry
        from unittest.mock import AsyncMock
        import src.memory.vector_store as vector_module
        from datetime import datetime, timezone

        m = SchemaManager()
        now = datetime.now(timezone.utc).isoformat()
        store = type("Store", (), {"get_by_filter": AsyncMock(return_value=[
            VectorEntry("table:demo.orders", "orders - 订单表", {
                "source": "auto_introspect", "category": "table",
                "table_name": "orders", "column_name": "", "tags": [""],
                "created_at": now, "ttl": str(AUTO_TTL_SECONDS), "meta_json": "{}"
            }),
        ])})()
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=store))
        m._initialized = True

        cached = await m._query_cache("demo")
        assert len(cached) == 1
        assert cached[0].id == "table:demo.orders"
        assert cached[0].table_name == "orders"
        filters = store.get_by_filter.await_args.args[0]
        assert filters["datasource"] == "demo"

    def test_singleton(self):
        from src.knowledge.schema_manager import get_schema_manager
        m1 = get_schema_manager()
        m2 = get_schema_manager()
        assert m1 is m2


class TestSchemaManagerManagement:
    """覆盖管理 API 使用的刷新与字段备注接口。"""

    async def test_refresh_deletes_old_cache_and_upserts_introspection(self, monkeypatch):
        """刷新应删除旧条目并把新的内省结果写回缓存。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.memory.vector_store as vector_module
        from src.knowledge.schema_manager import SchemaManager

        manager = SchemaManager()
        manager._initialized = True
        old_entry = KnowledgeEntry(
            "table:demo.old", "old", KnowledgeSource.AUTO_INTROSPECT,
            "table", table_name="old",
        )
        new_entry = KnowledgeEntry(
            "table:demo.orders", "orders - 订单表", KnowledgeSource.AUTO_INTROSPECT,
            "table", table_name="orders",
        )
        monkeypatch.setattr(manager, "_query_cache", AsyncMock(return_value=[old_entry]))
        monkeypatch.setattr(manager, "_introspect_from_db", AsyncMock(return_value=[new_entry]))
        monkeypatch.setattr(manager, "_upsert_to_cache", AsyncMock())
        store = type("Store", (), {"delete_by_ids": AsyncMock(return_value=1)})()
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=store))

        # Act
        snapshot = await manager.refresh("demo")

        # Assert
        store.delete_by_ids.assert_awaited_once_with(["table:demo.old"])
        manager._upsert_to_cache.assert_awaited_once_with([new_entry])
        assert [table.name for table in snapshot.tables] == ["orders"]

    async def test_update_column_comment_upserts_changed_content(self, monkeypatch):
        """字段备注更新应重写指定字段的向量文本。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.memory.vector_store as vector_module
        from src.knowledge.schema_manager import SchemaManager
        from src.memory.vector_store import VectorEntry

        manager = SchemaManager()
        manager._initialized = True
        entry = VectorEntry(
            id="column:demo.orders.amount",
            content="orders.amount: Decimal",
            metadata={"type": "Decimal", "tenant_id": 1},
        )
        store = type("Store", (), {
            "get_by_id": AsyncMock(return_value=entry),
            "upsert": AsyncMock(return_value=1),
        })()
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=store))

        # Act
        updated = await manager.update_column_comment("demo", "orders", "amount", "订单金额")

        # Assert
        assert updated is True
        saved = store.upsert.await_args.args[0][0]
        assert saved.id == "column:demo.orders.amount"
        assert "订单金额" in saved.content
