"""EmbeddedDataSourceProvider 测试 — 2.2.3~10 ORM 检测 + Schema 提取。"""

from __future__ import annotations

import asyncio
import os

import pytest


class TestORMDetection:
    """2.2.3-2.2.7 ORM 检测。"""

    def test_is_django_project_false(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "django", None)
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        assert EmbeddedDataSourceProvider()._is_django_project() is False  # noqa: SLF001

    def test_has_sqlalchemy_true(self):
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        assert EmbeddedDataSourceProvider()._has_sqlalchemy_engine() is True  # noqa: SLF001

    def test_has_sqlalchemy_false(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        assert EmbeddedDataSourceProvider()._has_sqlalchemy_engine() is False  # noqa: SLF001


class TestModelDescription:
    """2.2.9 ORM Model 描述提取。"""

    def test_from_docstring(self):
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        class M:
            __doc__ = "用户订单表\n包含所有订单记录"
        desc = EmbeddedDataSourceProvider()._extract_model_description(M)  # noqa: SLF001
        assert desc == "用户订单表"

    def test_empty_without_docstring(self):
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        class M:
            pass
        assert EmbeddedDataSourceProvider()._extract_model_description(M) == ""  # noqa: SLF001

    def test_find_orm_models_returns_dict(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "django", None)
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        models = EmbeddedDataSourceProvider()._find_orm_models()  # noqa: SLF001
        assert isinstance(models, dict)


class TestEnvDiscovery:
    """2.2.2 环境变量发现。"""

    def test_single_source(self, monkeypatch):
        monkeypatch.setenv("DATASOURCE_NAME", "ch")
        monkeypatch.setenv("DATASOURCE_DIALECT", "clickhouse")
        monkeypatch.setenv("DATASOURCE_HOST", "10.0.0.1")
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        sources = asyncio.run(EmbeddedDataSourceProvider().list_all())
        assert any(s.name == "ch" for s in sources)

    def test_multi_source(self, monkeypatch):
        monkeypatch.setenv("DATASOURCE_0_NAME", "ch1")
        monkeypatch.setenv("DATASOURCE_0_DIALECT", "clickhouse")
        monkeypatch.setenv("DATASOURCE_1_NAME", "pg1")
        monkeypatch.setenv("DATASOURCE_1_DIALECT", "postgres")
        from src.datasource.providers.embedded import EmbeddedDataSourceProvider
        sources = asyncio.run(EmbeddedDataSourceProvider().list_all())
        names = {s.name for s in sources}
        assert "ch1" in names
        assert "pg1" in names


class TestNormalizeDialect:
    """_normalize_dialect() 方言名归一化。"""

    @pytest.mark.parametrize("engine,expected", [
        ("django_clickhouse.backend", "clickhouse"),
        ("clickhouse+asynch", "clickhouse"),
        ("django.db.backends.postgresql", "postgres"),
        ("postgresql+asyncpg", "postgres"),
        ("django.db.backends.mysql", "mysql"),
        ("mysql+aiomysql", "mysql"),
        ("sqlite3", "postgres"),
        ("unknown_engine", "postgres"),
    ])
    def test_normalize(self, engine, expected):
        from src.datasource.providers.embedded import _normalize_dialect
        assert _normalize_dialect(engine) == expected
