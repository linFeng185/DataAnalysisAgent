"""25.4 连接器启停与声明验证。"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class TestHotPlugins:
    """覆盖动态目录、停用、恢复和未知方言。"""

    # 验证真实发行包元数据可在解释器运行期间被发现、使用及移除。
    # Args: self - 实例；tmp_path - 隔离发行包目录；monkeypatch - 模块搜索路径工具。
    # Returns: 无返回值。
    @pytest.mark.integration
    async def test_live_distribution_discovery(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """使用真实 importlib.metadata 和本地 SQLite 驱动，不模拟 entry_points。"""
        from src.connectors import plugins
        from src.connectors.registry import create_connector
        from src.datasource.config import DataSourceConfig
        from src.datasource.introspection import introspect_database

        # Arrange：仅在临时目录构造可信测试发行包，不安装到项目环境。
        monkeypatch.syspath_prepend(str(tmp_path))
        module_name = "runtime_fixture_connector"
        distribution = tmp_path / "runtime_fixture_connector-1.0.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text("Metadata-Version: 2.1\nName: runtime-fixture-connector\nVersion: 1.0\n", encoding="utf-8")
        (distribution / "entry_points.txt").write_text("[data_analysis_agent.connectors]\nruntimefixture = runtime_fixture_connector:RuntimeFixtureConnector\n", encoding="utf-8")
        (tmp_path / f"{module_name}.py").write_text(
            'from src.connectors.sqlite import SQLiteConnector\n'
            'from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema\n'
            'class RuntimeFixtureConnector(SQLiteConnector):\n'
            '    """复用 SQLite 的隔离测试插件。"""\n'
            '    sqlglot_dialect = "sqlite"\n'
            '    connection_fields = ["database"]\n'
            '    # 通过真实驱动读取元数据，验证发现到执行链路。\n'
            '    # Args: self - 插件；executor/table_names - 元数据执行器及可选表名。\n'
            '    # Returns: 数据库中已存在的表快照。\n'
            '    async def introspect_schema(self, *, executor, table_names=None):\n'
            '        rows = await executor.execute("SELECT name FROM sqlite_master WHERE type=\'table\'")\n'
            '        return SchemaSnapshot([TableSchema(row["name"]) for row in rows])\n',
            encoding="utf-8",
        )
        connector = None
        try:
            # Act：同一进程发现、建连、执行、内省、停用、恢复和卸载。
            catalog = plugins.discover_plugins()
            assert any(item["dialect"] == "runtimefixture" and item["external"] for item in catalog)
            source = DataSourceConfig(name="fixture", dialect="runtimefixture", mode="external", database=":memory:")
            connector = create_connector(source)
            source.connector = connector
            source.engine = await connector.create_engine()
            assert await connector.execute("SELECT 7 AS value") == [{"value": 7}]
            assert (await introspect_database(source, connector)).tables == []
            plugins.apply_plugin_policy({"runtimefixture": False})
            with pytest.raises(ValueError, match="停用"):
                await connector.execute("SELECT 7 AS value")
            plugins.apply_plugin_policy({})
            assert await connector.execute("SELECT 8 AS value") == [{"value": 8}]
            (distribution / "entry_points.txt").unlink()
            plugins.discover_plugins()
            # Assert：已有连接实例也不能继续接受卸载后的新查询。
            with pytest.raises(ValueError, match="不支持"):
                await connector.execute("SELECT 9 AS value")
        finally:
            if connector is not None:
                await connector.close()
            (distribution / "entry_points.txt").unlink(missing_ok=True)
            plugins.discover_plugins()
            plugins.apply_plugin_policy({})
            sys.modules.pop(module_name, None)

    # 验证禁用不允许新连接，恢复不需要重启解释器。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_disable_and_enable(self) -> None:
        """内置连接器也服从平台插件状态。"""
        from src.connectors.plugins import apply_plugin_policy, plugin_catalog
        from src.connectors.registry import create_connector
        from src.datasource.config import DataSourceConfig

        assert any(item["dialect"] == "sqlite" for item in plugin_catalog())
        try:
            apply_plugin_policy({"sqlite": False})
            with pytest.raises(ValueError):
                create_connector(DataSourceConfig(name="test", dialect="sqlite", mode="external", database=":memory:"))
        finally:
            apply_plugin_policy({})
        assert create_connector(DataSourceConfig(name="test", dialect="sqlite", mode="external", database=":memory:"))

    # 验证发行包入口可热发现、SQL 映射、插件内省和卸载后撤权。
    # Args: self - 实例；monkeypatch - 发行包目录替身。
    # Returns: 无返回值。
    async def test_discover_introspect_and_uninstall(self, monkeypatch) -> None:
        """不需修改内置枚举或重启服务即可使用可信扩展类型。"""
        from src.api.schemas import DataSourceCreateRequest
        from src.connectors import plugins
        from src.connectors.registry import create_connector
        from src.connectors.sqlite import SQLiteConnector
        from src.datasource.config import DataSourceConfig
        from src.datasource.introspection import introspect_database
        from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema
        class CustomConnector(SQLiteConnector):
            """测试发行包连接器，复用本地驱动但不实际访问它。"""
            sqlglot_dialect = "sqlite"
            connection_fields = ["database"]
            # 返回独立插件声明的表结构。
            # Args: self - 实例；executor/table_names - 调用契约。
            # Returns: 插件快照。
            async def introspect_schema(self, *, executor, table_names=None):
                return SchemaSnapshot([TableSchema("plugin_table")])
        entry = SimpleNamespace(name="customdb", load=lambda: CustomConnector)
        monkeypatch.setattr(plugins.metadata, "entry_points", lambda **kwargs: [entry])
        try:
            plugins.discover_plugins()
            assert plugins.sql_dialect("customdb") == "sqlite"
            assert DataSourceCreateRequest(name="custom", dialect="customdb").dialect == "customdb"
            source = DataSourceConfig(name="custom", dialect="customdb", mode="external")
            connector = create_connector(source)
            source.connector = connector
            assert (await introspect_database(source, AsyncMock())).tables[0].name == "plugin_table"
            monkeypatch.setattr(plugins.metadata, "entry_points", lambda **kwargs: [])
            plugins.discover_plugins()
            with pytest.raises(ValueError):
                await connector.execute("SELECT 1")
            assert "customdb" not in {item["dialect"] for item in plugins.plugin_catalog()}
        finally:
            monkeypatch.setattr(plugins.metadata, "entry_points", lambda **kwargs: [])
            plugins.discover_plugins()
            plugins.apply_plugin_policy({})

    # 验证错误插件不会覆盖内置类型或破坏其他连接器。
    # Args: self - 实例；monkeypatch - 插件目录替身。
    # Returns: 无返回值。
    def test_invalid_plugin_is_visible_but_unavailable(self, monkeypatch) -> None:
        """未知类或未实现内省的插件不能进入可用目录。"""
        from src.connectors import plugins
        monkeypatch.setattr(plugins.metadata, "entry_points", lambda **kwargs: [SimpleNamespace(name="broken", load=lambda: object)])
        try:
            catalog = plugins.discover_plugins()
            broken = next(item for item in catalog if item["dialect"] == "broken")
            assert not broken["enabled"] and broken["error"]
            assert any(item["dialect"] == "sqlite" and item["enabled"] for item in catalog)
        finally:
            monkeypatch.setattr(plugins.metadata, "entry_points", lambda **kwargs: [])
            plugins.discover_plugins()
    # 验证已经缓存的连接器不绕过后续停用策略。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_cached_execution_is_revoked(self) -> None:
        """禁用在进入数据库驱动之前阻断旧实例。"""
        from src.connectors.plugins import apply_plugin_policy
        from src.connectors.registry import create_connector
        from src.datasource.config import DataSourceConfig
        connector = create_connector(DataSourceConfig(name="test", dialect="sqlite", mode="external", database=":memory:"))
        try:
            apply_plugin_policy({"sqlite": False})
            with pytest.raises(ValueError):
                await connector.execute("SELECT 1")
            await connector.close()
        finally:
            apply_plugin_policy({})
