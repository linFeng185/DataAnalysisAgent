"""真实连接器多数据源验收测试。"""

from __future__ import annotations

import os
import sqlite3

import pytest


class TestLocalSQLiteMultiSourceAcceptance:
    """覆盖功能 15.8 的本地真实双数据源查询与合并。"""

    @pytest.mark.integration
    async def test_two_real_sqlite_connectors_execute_and_merge(self, tmp_path, monkeypatch) -> None:
        """两个独立 SQLite 文件应真实查询，并由多源合并节点保留来源。"""
        # Arrange
        from src.connectors.sqlite import SQLiteConnector
        from src.datasource.config import DataSourceConfig
        from src.graph.nodes.multi_source import merge_results_node

        connectors = []
        source_results = []
        for name, values in (("current", [10.0, 20.0]), ("archive", [5.0, 15.0])):
            database = tmp_path / f"{name}.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE sales (amount REAL NOT NULL)")
                connection.executemany("INSERT INTO sales(amount) VALUES (?)", [(value,) for value in values])
                connection.commit()
            finally:
                connection.close()
            config = DataSourceConfig(
                name=name,
                dialect="sqlite",
                mode="external",
                database=str(database),
                extra_params={"db_path": str(database)},
            )
            connector = SQLiteConnector(config)
            connectors.append(connector)
            rows, truncated = await connector.execute_bounded(
                "SELECT ROUND(SUM(amount), 2) AS total_sales FROM sales",
                max_rows=10,
            )
            source_results.append({
                "datasource": name,
                "success": True,
                "dialect": "sqlite",
                "sql": "SELECT ROUND(SUM(amount), 2) AS total_sales FROM sales",
                "data": rows,
                "full_count": len(rows),
                "truncated": truncated,
            })
        import src.llm.client as llm_client

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        try:
            # Act
            merged = await merge_results_node({
                "user_query": "对比各数据源销售额",
                "intent": "aggregation",
                "selected_datasources": ["current", "archive"],
                "multi_source_results": source_results,
            })
        finally:
            for connector in connectors:
                await connector.close()

        # Assert
        rows = merged["query_result_sample"]
        assert {row["_datasource"] for row in rows} == {"current", "archive"}
        assert {str(row["total_sales"]) for row in rows} == {"30.0", "20.0"}
        assert merged["query_result_full_count"] == 2


class TestExternalMultiSourceAcceptance:
    """显式开关控制的真实外部数据源连接验收。"""

    @pytest.mark.live_datasource
    async def test_configured_external_sources_are_reachable(self) -> None:
        """至少两个显式配置的数据源应能通过各自连接器健康检查。"""
        # Arrange
        if os.getenv("RUN_LIVE_DATASOURCE_TESTS") != "1":
            pytest.skip("需要 RUN_LIVE_DATASOURCE_TESTS=1")
        names = [
            item.strip()
            for item in os.getenv("LIVE_DATASOURCE_NAMES", "").split(",")
            if item.strip()
        ]
        if len(names) < 2:
            pytest.skip("LIVE_DATASOURCE_NAMES 至少需要两个数据源")
        from src.datasource.registry import get_registry

        registry = get_registry()

        # Act
        resolved = [await registry.resolve(name) for name in names]
        health = [await config.connector.health_check() for config in resolved]

        # Assert
        assert all(health)
