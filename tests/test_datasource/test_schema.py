"""数据源模块单元测试 — Schema 数据结构 + 凭证 + Provider + Registry。"""

from __future__ import annotations

import asyncio
import os

import pytest


class TestSchemaDataStructures:
    """2.6.1-6 SchemaSnapshot / TableSchema / ColumnInfo / TableRelation."""

    def test_column_info(self):
        from src.datasource.schema_snapshot import ColumnInfo

        c = ColumnInfo(name="id", type="UInt64")
        assert c.name == "id"
        assert c.comment == ""

    def test_table_relation(self):
        from src.datasource.schema_snapshot import TableRelation

        r = TableRelation(target_table="users", join_key="user_id", relation_type="many_to_one")
        assert r.target_table == "users"

    def test_table_schema(self):
        from src.datasource.schema_snapshot import ColumnInfo, TableSchema

        t = TableSchema(
            name="orders", description="订单表",
            columns=[ColumnInfo(name="id", type="UInt64")],
            row_count_estimate=1000, partition_key="toYYYYMM(created_at)",
        )
        assert len(t.columns) == 1
        assert t.row_count_estimate == 1000
        assert "YYYYMM" in t.partition_key

    def test_schema_snapshot_to_prompt_text(self):
        from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema

        ss = SchemaSnapshot(
            tables=[TableSchema(
                name="users", description="用户表",
                columns=[ColumnInfo(name="id", type="Int64", comment="用户ID")],
                row_count_estimate=50000,
            )],
            field_semantics=[{"content": "users.name 为真实姓名"}],
            business_rules=[{"content": "GMV = SUM(amount) WHERE status=2"}],
            sql_templates=[{"content": "上月GMV", "sql": "SELECT SUM(amount) ..."}],
        )
        text = ss.to_prompt_text()
        assert "## 数据库表结构" in text
        assert "users" in text
        assert "用户表" in text
        assert "估算行数: 50,000" in text
        assert "## 关键字段说明" in text
        assert "## 业务规则" in text
        assert "## 相似问题参考" in text

    def test_merge_new_table(self):
        from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema

        a = SchemaSnapshot(tables=[TableSchema(name="t1", columns=[ColumnInfo(name="a", type="Int")])])
        b = SchemaSnapshot(tables=[TableSchema(name="t2", columns=[ColumnInfo(name="b", type="String")])])
        a.merge(b)
        assert len(a.tables) == 2

    def test_merge_same_table_preserves_description(self):
        from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema

        a = SchemaSnapshot(tables=[TableSchema(name="t1", description="ORM desc", columns=[ColumnInfo(name="a", type="Int")])])
        b = SchemaSnapshot(tables=[TableSchema(name="t1", description="", columns=[ColumnInfo(name="b", type="String")], row_count_estimate=100)])
        a.merge(b)
        t = a.tables[0]
        assert t.description == "ORM desc"
        assert t.row_count_estimate == 100
        assert len(t.columns) == 2


class TestCredentialManager:
    """2.4.1-2 CredentialManager."""

    def test_encrypt_decrypt(self):
        from src.datasource.credential_manager import CredentialManager

        cm = CredentialManager(key="test_key_32_bytes_long_phrase!")
        encrypted = cm.encrypt("my_secret_password")
        assert encrypted != "my_secret_password"
        assert cm.decrypt(encrypted) == "my_secret_password"

    def test_resolve_env_ref(self):
        from src.datasource.credential_manager import CredentialManager

        os.environ["TEST_DB_PASS"] = "resolved_value"
        result = CredentialManager.resolve_env_ref("user:${TEST_DB_PASS}@host")
        assert result == "user:resolved_value@host"


class TestDataSourceConfig:
    """2.1.1 DataSourceConfig."""

    def test_minimal(self):
        from src.datasource.config import DataSourceConfig

        ds = DataSourceConfig(name="test", dialect="clickhouse", mode="embedded")
        assert ds.name == "test"
        assert ds.host == "localhost"

    def test_full(self):
        from src.datasource.config import DataSourceConfig

        ds = DataSourceConfig(
            name="prod_ch", dialect="clickhouse", mode="embedded",
            host="10.0.1.100", port=9000, database="analytics",
            username="reader", description="生产", tags=["只读"],
        )
        assert ds.port == 9000
        assert "只读" in ds.tags


class TestEmbeddedProvider:
    """2.2.1 EmbeddedDataSourceProvider."""

    def test_env_discovery(self, monkeypatch):
        monkeypatch.setenv("DATASOURCE_NAME", "demo_ch")
        monkeypatch.setenv("DATASOURCE_DIALECT", "clickhouse")
        monkeypatch.setenv("DATASOURCE_HOST", "10.0.1.100")
        monkeypatch.setenv("DATASOURCE_PORT", "9000")
        monkeypatch.setenv("DATASOURCE_DATABASE", "analytics")

        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        p = EmbeddedDataSourceProvider()
        assert len(list(p._sources.values())) >= 1  # noqa: SLF001


class TestDataSourceRegistry:
    """2.1.2 DataSourceRegistry."""

    def test_resolve_not_found(self):
        from src.datasource.registry import DataSourceRegistry
        from src.exceptions import DataSourceNotFoundError

        registry = DataSourceRegistry()
        with pytest.raises(DataSourceNotFoundError):
            asyncio.run(registry.resolve("nonexistent"))

    def test_list_all_empty(self):
        from src.datasource.registry import DataSourceRegistry
        assert asyncio.run(DataSourceRegistry().list_all()) == []

    def test_register_provider(self):
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        from src.datasource.registry import DataSourceRegistry

        registry = DataSourceRegistry()
        registry.register_provider("embedded", EmbeddedDataSourceProvider())
        assert len(registry._providers) == 1  # noqa: SLF001
