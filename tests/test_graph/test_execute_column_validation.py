"""执行前列引用方言与失败关闭回归测试。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TestColumnReferenceValidation:
    """覆盖 execute_sql 列引用校验方言和异常路径。"""

    # 方法作用：验证 MSSQL 列校验使用 sqlglot 的 tsql 方言。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mssql_uses_tsql_parser(self, monkeypatch) -> None:
        """列引用解析不得硬编码为 MySQL。"""
        logger.debug("test_mssql_uses_tsql_parser 入口")
        try:
            # Arrange
            import sqlglot

            from src.graph.nodes.execute_sql import _validate_column_references

            captured: dict[str, str] = {}
            original = sqlglot.parse_one

            def capture_parse(sql: str, *, read: str):
                captured["read"] = read
                return original(sql, read=read)

            monkeypatch.setattr(sqlglot, "parse_one", capture_parse)

            # Act
            result = _validate_column_references(
                "SELECT [Order Date] FROM orders",
                [{"name": "orders", "columns": [{"name": "Order Date"}]}],
                "mssql",
            )

            # Assert
            assert result is None
            assert captured["read"] == "tsql"
            logger.info("test_mssql_uses_tsql_parser 完成")
        except Exception as exc:
            logger.error("test_mssql_uses_tsql_parser 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证列引用解析异常返回受控错误而不是默认放行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_parser_failure_is_closed(self, monkeypatch) -> None:
        """执行层二次列校验异常必须阻断 SQL。"""
        logger.debug("test_parser_failure_is_closed 入口")
        try:
            # Arrange
            import sqlglot

            from src.graph.nodes.execute_sql import _validate_column_references

            monkeypatch.setattr(
                sqlglot,
                "parse_one",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("parse failed")),
            )

            # Act
            result = _validate_column_references(
                "SELECT amount FROM orders",
                [{"name": "orders", "columns": [{"name": "amount"}]}],
                "postgres",
            )

            # Assert
            assert result is not None
            assert "解析失败" in result
            logger.info("test_parser_failure_is_closed 完成")
        except Exception as exc:
            logger.error("test_parser_failure_is_closed 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证跨表列错配探针能识别“列名只在别的表存在”的漏检形态。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cross_table_column_probe(self) -> None:
        """列不属于本语句引用的表时，探针必须指出真正提供该列的表。"""
        import sqlglot

        from src.graph.nodes.execute_sql import find_cross_table_columns

        # Arrange：created_at 只存在于 audit_log，却被挂在 finance_invoices 的别名 i 上
        tables = [
            {"name": "finance_invoices", "columns": [{"name": "event_date"}, {"name": "year_month"}]},
            {"name": "audit_log", "columns": [{"name": "created_at"}]},
        ]
        sql = "SELECT DATE_TRUNC('MONTH', \"i\".\"created_at\") FROM \"finance_invoices\" AS \"i\""

        # Act：直接测检测逻辑，不依赖日志管道
        mismatches = find_cross_table_columns(sqlglot.parse_one(sql, read="postgres"), tables, set())

        # Assert
        assert len(mismatches) == 1
        assert mismatches[0]["column"] == "i.created_at"
        assert mismatches[0]["referenced_table"] == "finance_invoices"
        assert mismatches[0]["provided_by"] == ["audit_log"]

    # 方法作用：验证列确实属于引用表时探针不误报。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cross_table_probe_ignores_matching_column(self) -> None:
        """列属于本语句引用的表时不得告警，避免把正常 SQL 当异常。"""
        import sqlglot

        from src.graph.nodes.execute_sql import find_cross_table_columns

        # Arrange
        tables = [{"name": "finance_invoices", "columns": [{"name": "event_date"}, {"name": "year_month"}]}]
        sql = 'SELECT "i"."year_month" FROM "finance_invoices" AS "i"'

        # Act
        mismatches = find_cross_table_columns(sqlglot.parse_one(sql, read="postgres"), tables, set())

        # Assert
        assert mismatches == []

    # 方法作用：验证列引用校验已前移到 EXPLAIN 之前，幻觉列在 layer3 就被拦截。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_layer3_blocks_hallucinated_column(self) -> None:
        """幻觉列必须在 layer3 失败，才能把可用列清单喂回重试，而不是等 EXPLAIN 打库。"""
        import asyncio

        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：year_month 可用，created_at 不可用
        state = {
            "generated_sql": 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            "relevant_tables": [{"name": "finance_invoices", "columns": [{"name": "year_month"}]}],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert：失败类型必须能被 after_layer3 路由回 generate_sql 重试
        assert result["sql_valid"] is False
        assert result["validation_errors"][0]["type"] == "column_reference"
        assert "created_at" in result["validation_errors"][0]["message"]

    # 方法作用：验证列合法时 layer3 仍然放行，不误伤正常 SQL。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_layer3_passes_when_columns_valid(self) -> None:
        """列属于引用表时 layer3 必须放行，否则会误伤全部正常查询。"""
        import asyncio

        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange
        state = {
            "generated_sql": 'SELECT "i"."year_month" FROM "finance_invoices" AS "i"',
            "relevant_tables": [{"name": "finance_invoices", "columns": [{"name": "year_month"}]}],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert
        assert result["sql_valid"] is True
