"""补充测试 — 边界条件、错误路径、跨方言兼容。覆盖现有「单测完成」项的遗漏场景。"""

from __future__ import annotations

import asyncio
import os

import pytest


# ================================================================
# SchemaSnapshot — 补充空快照、重复合并
# ================================================================

class TestSchemaSnapshotEdgeCases:
    """2.6.1~3 补充。"""

    def test_to_prompt_text_empty(self):
        """边界条件: 空 SchemaSnapshot 不抛异常。"""
        from src.datasource.schema_snapshot import SchemaSnapshot
        text = SchemaSnapshot().to_prompt_text()
        assert isinstance(text, str)

    def test_merge_duplicate_relations(self):
        """边界条件: 同表多次 merge 关系追加不覆盖。"""
        from src.datasource.schema_snapshot import (
            ColumnInfo, SchemaSnapshot, TableRelation, TableSchema,
        )
        a = SchemaSnapshot(tables=[TableSchema(
            name="t", relations=[TableRelation("u1", "id", "many_to_one")]
        )])
        b = SchemaSnapshot(tables=[TableSchema(
            name="t", relations=[TableRelation("u2", "id", "many_to_one")]
        )])
        a.merge(b)
        assert len(a.tables[0].relations) == 2

    def test_to_prompt_text_without_optional_sections(self):
        """边界条件: 仅表结构时不含业务规则标题。"""
        from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
        ss = SchemaSnapshot(tables=[TableSchema(
            name="t", columns=[ColumnInfo(name="a", type="int")]
        )])
        text = ss.to_prompt_text()
        assert "## 数据库表结构" in text
        assert "## 关键字段说明" not in text
        assert "## 业务规则" not in text


# ================================================================
# CredentialManager — 补充 decrypt 边界
# ================================================================

class TestCredentialManagerEdgeCases:
    """2.4.1~2 补充。"""

    def test_decrypt_empty_string(self):
        """边界条件: 空字符串解密返回空。"""
        from src.datasource.credential_manager import CredentialManager
        cm = CredentialManager(key="32_byte_key_for_testing_abcde!")
        assert cm.decrypt("") == ""

    def test_special_characters(self):
        """边界条件: 含特殊字符的密码。"""
        from src.datasource.credential_manager import CredentialManager
        cm = CredentialManager(key="MySecretKey123456789012345678!")
        pw = "p@$$w0rd!#%^&*()"
        assert cm.decrypt(cm.encrypt(pw)) == pw

    def test_resolve_no_match(self):
        """边界条件: 无占位符原样返回。"""
        from src.datasource.credential_manager import CredentialManager
        assert CredentialManager.resolve_env_ref("no_var") == "no_var"

    def test_resolve_unset_var(self):
        """边界条件: 未设置环境变量保留占位符。"""
        from src.datasource.credential_manager import CredentialManager
        result = CredentialManager.resolve_env_ref("${NONEXISTENT_VAR}")
        assert "${NONEXISTENT_VAR}" in result


# ================================================================
# DataSourceConfig — 补充默认值
# ================================================================

class TestDataSourceConfigEdgeCases:
    """2.1.1 补充。"""

    def test_password_default_empty(self):
        """边界条件: password 默认空。"""
        from src.datasource.config import DataSourceConfig
        assert DataSourceConfig(name="t", dialect="mysql", mode="embedded").password == ""

    def test_tags_extra_params_defaults(self):
        """边界条件: tags/extra_params 默认为空。"""
        from src.datasource.config import DataSourceConfig
        ds = DataSourceConfig(name="t", dialect="clickhouse", mode="external")
        assert ds.tags == []
        assert ds.extra_params == {}


# ================================================================
# EmbeddedProvider — 补充多数据源、空环境
# ================================================================

class TestEmbeddedProviderEdgeCases:
    """2.2.1~2 补充。"""

    def test_multi_source(self, monkeypatch):
        """正常路径: DATASOURCE_0 + DATASOURCE_1。"""
        monkeypatch.setenv("DATASOURCE_0_NAME", "ch_prod")
        monkeypatch.setenv("DATASOURCE_0_DIALECT", "clickhouse")
        monkeypatch.setenv("DATASOURCE_0_HOST", "10.0.1.1")
        monkeypatch.setenv("DATASOURCE_1_NAME", "pg_analytics")
        monkeypatch.setenv("DATASOURCE_1_DIALECT", "postgres")
        monkeypatch.setenv("DATASOURCE_1_HOST", "10.0.2.1")

        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        sources = asyncio.run(EmbeddedDataSourceProvider().list_all())
        names = {s.name for s in sources}
        assert "ch_prod" in names
        assert "pg_analytics" in names

    def test_no_env_configured(self, monkeypatch):
        """边界条件: 无环境变量时 Provider 为空。"""
        for key in list(os.environ):
            if key.startswith("DATASOURCE"):
                monkeypatch.delenv(key, raising=False)

        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        sources = asyncio.run(EmbeddedDataSourceProvider().list_all())
        assert sources == []

    def test_lookup_nonexistent(self, monkeypatch):
        """边界条件: 查询不存在的源返回 None。"""
        for key in list(os.environ):
            if key.startswith("DATASOURCE"):
                monkeypatch.delenv(key, raising=False)

        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        result = asyncio.run(EmbeddedDataSourceProvider().lookup("nope"))
        assert result is None


# ================================================================
# DataSourceRegistry — 补充 resolve_or_none
# ================================================================

class TestDataSourceRegistryEdgeCases:
    """2.1.2 补充。"""

    def test_resolve_or_none_not_found(self):
        """正常路径: 不存在返回 None 不抛异常。"""
        from src.datasource.registry import DataSourceRegistry
        assert asyncio.run(DataSourceRegistry().resolve_or_none("x")) is None

    def test_multiple_providers_iteration(self):
        """正常路径: 遍历所有 Provider 直到匹配。"""
        from src.datasource.registry import DataSourceRegistry

        class EmptyProvider:
            def __init__(self, name):
                self._sources = {}
            async def lookup(self, name):
                return None
            async def list_all(self):
                return []
            async def extract_schema(self, ds):
                from src.datasource.schema_snapshot import SchemaSnapshot
                return SchemaSnapshot()
            async def test_connection(self, ds):
                return True

        reg = DataSourceRegistry()
        reg.register_provider("a", EmptyProvider("a"))
        reg.register_provider("b", EmptyProvider("b"))
        assert asyncio.run(reg.resolve_or_none("x")) is None


# ================================================================
# 跨方言兼容验证
# ================================================================

class TestDialectCompatibility:
    """验证常量字典覆盖所有目标方言。"""

    def test_columns_query_coverage(self):
        from src.datasource.introspection import COLUMNS_QUERY
        for d in ("clickhouse", "mysql", "postgres"):
            assert d in COLUMNS_QUERY, f"缺少方言: {d}"

    def test_fk_query_coverage(self):
        from src.datasource.introspection import FK_QUERY
        assert "mysql" in FK_QUERY
        assert "postgres" in FK_QUERY
        assert FK_QUERY.get("clickhouse") is None

    def test_row_count_query_coverage(self):
        from src.datasource.introspection import ROW_COUNT_QUERY
        for d in ("clickhouse", "mysql", "postgres"):
            assert d in ROW_COUNT_QUERY, f"缺少方言: {d}"

    def test_dialect_defaults_port(self):
        from src.datasource.providers.embedded import _DIALECT_DEFAULTS
        assert _DIALECT_DEFAULTS["clickhouse"] == 9000
        assert _DIALECT_DEFAULTS["mysql"] == 3306
        assert _DIALECT_DEFAULTS["postgres"] == 5432
