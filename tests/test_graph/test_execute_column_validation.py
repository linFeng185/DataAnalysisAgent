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
