"""Schema 元数据权限告警测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.datasource.config import DataSourceConfig


@pytest.mark.asyncio
class TestSchemaPermissionWarning:
    """覆盖功能 2.5.10 的权限异常识别和知识告警。"""

    # 方法作用：验证表级元数据权限异常不会被内省循环静默吞掉。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_introspection_promotes_metadata_permission_error(self) -> None:
        """PostgreSQL 42501 应提升为统一 MetadataPermissionError。"""
        # Arrange
        from src.datasource.introspection import MetadataPermissionError, introspect_database

        datasource = DataSourceConfig(name="sales", dialect="postgres", mode="external")

        # 方法作用：模拟先列表成功、读取字段时被数据库拒绝。
        # Args: datasource_config - 数据源；sql - 元数据 SQL；params - 查询参数。
        # Returns: 表列表；字段查询抛出权限异常。
        async def executor(datasource_config, sql, params):
            del datasource_config, params
            if "pg_catalog.pg_tables" in sql:
                return [{"name": "orders"}]
            error = PermissionError("permission denied for relation information_schema.columns")
            error.sqlstate = "42501"
            raise error

        # Act / Assert
        with pytest.raises(MetadataPermissionError) as caught:
            await introspect_database(datasource, executor)
        assert caught.value.datasource == "sales"
        assert caught.value.operation == "columns"

    # 方法作用：验证 SchemaManager 将统一权限异常写为 SYSTEM_WARNING 并上报指标。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_manager_persists_system_warning(self, monkeypatch) -> None:
        """权限不足时用户应能在知识告警和 Prometheus 中看到信号。"""
        # Arrange
        import src.datasource.introspection as introspection
        import src.datasource.registry as registry_module
        import src.memory.vector_store as vector_module
        import src.observability as observability
        from src.knowledge.schema_manager import SchemaManager

        datasource = DataSourceConfig(name="sales", dialect="postgres", mode="external")
        registry = SimpleNamespace(resolve=AsyncMock(return_value=datasource))
        store = SimpleNamespace(upsert=AsyncMock(return_value=1))
        metrics = SimpleNamespace(record_schema_warning=MagicMock())
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=store))
        monkeypatch.setattr(observability, "get_metrics_registry", lambda: metrics)
        monkeypatch.setattr(
            introspection,
            "introspect_database",
            AsyncMock(side_effect=introspection.MetadataPermissionError(
                datasource="sales",
                dialect="postgres",
                operation="tables",
            )),
        )
        manager = SchemaManager(datasource_cache=SimpleNamespace())
        monkeypatch.setattr(manager, "_ensure_external_provider", lambda: None)

        # Act
        entries = await manager._introspect_from_db("sales")  # noqa: SLF001

        # Assert
        assert entries == []
        warning = store.upsert.await_args.args[0][0]
        assert warning.metadata["source"] == "system_warning"
        assert warning.metadata["category"] == "system_warning"
        assert warning.metadata["datasource"] == "sales"
        metrics.record_schema_warning.assert_called_once_with("postgres", "tables")
