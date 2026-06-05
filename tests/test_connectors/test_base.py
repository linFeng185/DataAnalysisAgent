"""连接器模块测试 — URL 构建、工厂、格式化、超时、EXPLAIN。"""

from __future__ import annotations

import pytest

from src.datasource.config import DataSourceConfig


def _ds(dialect: str, **kw) -> DataSourceConfig:
    return DataSourceConfig(name=f"t_{dialect}", dialect=dialect, mode="embedded", **kw)


# ================================================================
# URL 构建 (3.1.2)
# ================================================================

class TestConnectionURL:

    def test_clickhouse(self):
        from src.connectors.clickhouse import ClickHouseConnector
        ds = _ds("clickhouse", host="ch.local", port=9000, database="analytics", username="r", password="s")
        url = ClickHouseConnector(ds)._build_url()  # noqa: SLF001
        assert url == "clickhouse+asynch://r:s@ch.local:9000/analytics"

    def test_mysql(self):
        from src.connectors.mysql import MySQLConnector
        ds = _ds("mysql", host="db1", port=3306, database="ecom", username="ro", password="p")
        url = MySQLConnector(ds)._build_url()  # noqa: SLF001
        assert "mysql+aiomysql://ro:p@db1:3306/ecom" in url
        assert "utf8mb4" in url

    def test_postgres(self):
        from src.connectors.postgres import PostgreSQLConnector
        ds = _ds("postgres", host="pg", port=5432, database="analytics", username="ro", password="p")
        url = PostgreSQLConnector(ds)._build_url()  # noqa: SLF001
        assert url == "postgresql+asyncpg://ro:p@pg:5432/analytics"


# ================================================================
# 工厂 (3.1)
# ================================================================

class TestConnectorFactory:

    @pytest.mark.parametrize("dialect,cls_name", [
        ("clickhouse", "ClickHouseConnector"),
        ("mysql", "MySQLConnector"),
        ("postgres", "PostgreSQLConnector"),
    ])
    def test_factory(self, dialect, cls_name):
        from src.connectors.base import create_connector
        c = create_connector(_ds(dialect))
        assert type(c).__name__ == cls_name

    def test_unsupported(self):
        from src.connectors.base import create_connector
        with pytest.raises(ValueError, match="不支持的方言"):
            create_connector(_ds("xxx"))


# ================================================================
# 结果格式化 (3.1.4)
# ================================================================

class TestRowsToDictList:

    def test_convert(self):
        from src.connectors.base import ConnectorBase
        class R:
            _mapping = {"a": 1}
        assert ConnectorBase.rows_to_dict_list([R()]) == [{"a": 1}]

    def test_empty(self):
        from src.connectors.base import ConnectorBase
        assert ConnectorBase.rows_to_dict_list([]) == []


# ================================================================
# 超时 SQL (3.1.3)
# ================================================================

class TestTimeoutSQL:

    def test_clickhouse(self):
        from src.connectors.clickhouse import ClickHouseConnector
        s = ClickHouseConnector(_ds("clickhouse"))._get_timeout()  # noqa: SLF001
        assert s and "max_execution_time" in s

    def test_mysql(self):
        from src.connectors.mysql import MySQLConnector
        s = MySQLConnector(_ds("mysql"))._get_timeout()  # noqa: SLF001
        assert s and "max_execution_time" in s

    def test_postgres(self):
        from src.connectors.postgres import PostgreSQLConnector
        s = PostgreSQLConnector(_ds("postgres"))._get_timeout()  # noqa: SLF001
        assert s and "statement_timeout" in s


# ================================================================
# EXPLAIN (3.4.2)
# ================================================================

class TestEXPLAIN:

    def test_clickhouse_skipped(self, monkeypatch):
        """explain_skip_dialects 配置生效。"""
        monkeypatch.setenv("EXPLAIN_SKIP_DIALECTS", '["clickhouse"]')
        import asyncio
        from src.connectors.clickhouse import ClickHouseConnector
        result = asyncio.run(
            ClickHouseConnector(_ds("clickhouse")).explain("SELECT 1")
        )
        assert result["valid"] is True


class TestClickHousePartition:
    """3.2.5 分区键。"""

    def test_no_db_graceful(self):
        import asyncio
        from src.connectors.clickhouse import ClickHouseConnector
        r = asyncio.run(ClickHouseConnector(_ds("clickhouse")).get_partition_key("t"))
        assert r == ""
