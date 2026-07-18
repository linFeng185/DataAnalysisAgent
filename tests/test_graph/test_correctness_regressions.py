"""SQLite、无 LLM 回退与分析路径正确性回归测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestNoLLMFallback:
    """覆盖 LLM 不可用时的确定性 SQL 回退。"""

    async def test_count_query_generates_count_star(self, monkeypatch):
        """明确数量问题应生成 COUNT(*)，不得退化为 SELECT *。"""
        # Arrange
        import src.graph.nodes.generate_sql as generate_module

        monkeypatch.setattr(generate_module, "is_llm_available", lambda: False)

        # Act
        result = await generate_module.generate_sql_node({
            "user_query": "统计订单数",
            "relevant_tables": [{"name": "orders", "columns": []}],
            "dialect": "sqlite",
        }, {})

        # Assert
        assert "COUNT(*)" in result["generated_sql"].upper()
        assert "SELECT *" not in result["generated_sql"].upper()

    async def test_uncertain_query_returns_explicit_error(self, monkeypatch):
        """无法确定语义时应停止执行并返回明确错误。"""
        # Arrange
        import src.graph.nodes.generate_sql as generate_module

        monkeypatch.setattr(generate_module, "is_llm_available", lambda: False)

        # Act
        result = await generate_module.generate_sql_node({
            "user_query": "分析订单异常原因",
            "relevant_tables": [{"name": "orders", "columns": []}],
            "dialect": "sqlite",
        }, {})

        # Assert
        assert result["generated_sql"] == ""
        assert "LLM" in result["execution_error"]


class TestSQLiteIntrospection:
    """覆盖 SQLite 专用系统表和 PRAGMA 路由。"""

    async def test_database_uses_sqlite_master_and_pragma(self):
        """SQLite 内省应读取 sqlite_master 和 PRAGMA table_info。"""
        # Arrange
        from src.datasource.config import DataSourceConfig
        from src.datasource.introspection import introspect_database

        statements: list[str] = []

        async def executor(ds, sql: str, params: dict):
            """按 SQLite 元数据语句返回受控结果。"""
            statements.append(sql)
            if "sqlite_master" in sql:
                return [{"name": "orders"}]
            if "PRAGMA table_info" in sql:
                return [
                    {"name": "id", "type": "INTEGER", "notnull": 1, "pk": 1},
                    {"name": "amount", "type": "REAL", "notnull": 0, "pk": 0},
                ]
            if "PRAGMA foreign_key_list" in sql:
                return []
            if "COUNT(*)" in sql:
                return [{"count": 2}]
            raise AssertionError(f"unexpected SQL: {sql}")

        datasource = DataSourceConfig(
            name="sqlite-test", dialect="sqlite", mode="embedded", database=":memory:",
        )

        # Act
        snapshot = await introspect_database(datasource, executor)

        # Assert
        assert [table.name for table in snapshot.tables] == ["orders"]
        assert snapshot.tables[0].columns[0].is_primary_key is True
        assert snapshot.tables[0].columns[0].is_nullable is False
        assert any("sqlite_master" in sql for sql in statements)
        assert any("PRAGMA table_info" in sql for sql in statements)

    async def test_registry_resolves_sqlite_with_aiosqlite_engine(self):
        """SQLite API 配置应创建 aiosqlite 引擎并通过连通性检查。"""
        # Arrange
        import sqlalchemy as sa

        from src.api.schemas import DataSourceCreateRequest
        from src.datasource.providers.external import ExternalDataSourceProvider
        from src.datasource.registry import DataSourceRegistry

        provider = ExternalDataSourceProvider()
        await provider.register(DataSourceCreateRequest(
            name="sqlite-local", dialect="sqlite", file_path=":memory:",
        ))
        registry = DataSourceRegistry()
        registry.register_provider("external", provider)

        # Act
        datasource = await registry.resolve("sqlite-local")
        async with datasource.engine.connect() as connection:
            value = await connection.scalar(sa.text("SELECT 1"))

        # Assert
        assert datasource.dialect == "sqlite"
        assert value == 1
        await datasource.engine.dispose()


class TestLLMAnalysisSampling:
    """覆盖 LLM 数据样本长度和标签计算。"""

    async def test_sampled_rows_do_not_compare_int_with_len_method(self, monkeypatch):
        """采样判断应比较实际列表长度，并在 Prompt 标记采样比例。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module
        import src.llm.adapters.registry as adapter_module

        response = SimpleNamespace(content=json.dumps({
            "summary": "ok",
            "insights": [],
            "recommended_chart_type": "table",
            "follow_up_questions": [],
        }))
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        monkeypatch.setattr(analyze_module, "get_llm", lambda temperature=0.3: llm)
        monkeypatch.setattr(analyze_module, "_to_compact", lambda rows: '[{"value":1}]')
        monkeypatch.setattr(
            adapter_module,
            "get_adapter",
            lambda model: SimpleNamespace(parse_response=lambda resp: SimpleNamespace(reasoning_content="")),
        )

        # Act
        result = await analyze_module._llm_analyze(
            [{"value": 1}, {"value": 2}], "SELECT value FROM t",
            {"columns": {}, "row_count": 2}, "", "", "",
        )

        # Assert
        assert result["summary"] == "ok"
        prompt = llm.ainvoke.await_args.args[0][1].content
        assert "均匀采样 1/2 行" in prompt
