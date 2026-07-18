"""SQL 只读白名单、权限关闭和审计安全测试。"""

from __future__ import annotations

import logging

import pytest


logger = logging.getLogger(__name__)


class TestReadonlySQLValidation:
    """覆盖 12.1 SQL 只读白名单。"""

    @pytest.mark.parametrize(
        "sql",
        ["CALL dangerous_proc()", "VACUUM", "SET ROLE admin", "DELETE FROM orders"],
    )
    def test_non_readonly_statements_are_rejected(self, sql):
        """非只读语句即使 sqlglot 能解析也必须被拒绝。"""
        # Arrange
        from src.graph.nodes.layer3_validate import validate_readonly_sql

        # Act
        errors = validate_readonly_sql(sql, "postgres")

        # Assert
        assert errors
        assert errors[0]["type"] == "security_block"

    def test_select_and_explain_select_are_allowed(self):
        """SELECT 与包裹 SELECT 的 EXPLAIN 应通过白名单。"""
        # Arrange
        from src.graph.nodes.layer3_validate import validate_readonly_sql

        # Act / Assert
        assert validate_readonly_sql("SELECT 1", "postgres") == []
        assert validate_readonly_sql("EXPLAIN SELECT 1", "postgres") == []

    def test_explain_delete_is_rejected(self):
        """EXPLAIN 不得用于绕过内部写语句校验。"""
        # Arrange
        from src.graph.nodes.layer3_validate import validate_readonly_sql

        # Act
        errors = validate_readonly_sql("EXPLAIN DELETE FROM orders", "postgres")

        # Assert
        assert errors
        assert errors[0]["type"] == "security_block"

    def test_invalid_sql_is_rejected(self):
        """解析失败必须关闭执行路径。"""
        # Arrange
        from src.graph.nodes.layer3_validate import validate_readonly_sql

        # Act
        errors = validate_readonly_sql("SELECT 'unterminated", "postgres")

        # Assert
        assert errors
        assert errors[0]["type"] == "syntax_error"

    # 验证应用方言名 mssql 会映射到 sqlglot 的 tsql 方言。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mssql_alias_is_validated_as_tsql(self):
        """合法的 SQL Server SELECT 不应因方言别名错误而校验失败。"""
        logger.debug("test_mssql_alias_is_validated_as_tsql 入口")
        try:
            # Arrange
            from src.graph.nodes.layer3_validate import validate_readonly_sql

            sql = "SELECT TOP 10 [id] FROM [customers]"

            # Act
            errors = validate_readonly_sql(sql, "mssql")

            # Assert
            assert errors == []
            logger.info("test_mssql_alias_is_validated_as_tsql 完成")
        except Exception as exc:
            logger.error("test_mssql_alias_is_validated_as_tsql 异常: %s", exc, exc_info=True)
            raise


class TestPermissionFailureClosed:
    """覆盖列权限与行过滤失败关闭。"""

    def test_column_wildcard_is_rejected_when_whitelist_is_enabled(self):
        """启用列白名单后不得用 SELECT * 绕过字段限制。"""
        # Arrange
        from src.security.permission_check import check_column_whitelist

        # Act
        error = check_column_whitelist("SELECT * FROM users", ["id"])

        # Assert
        assert error is not None
        assert "通配符" in error

    def test_count_wildcard_is_allowed_when_whitelist_is_enabled(self):
        """COUNT(*) 不返回未授权字段内容，应继续允许。"""
        # Arrange
        from src.security.permission_check import check_column_whitelist

        # Act
        error = check_column_whitelist("SELECT COUNT(*) FROM users", ["id"])

        # Assert
        assert error is None

    def test_empty_sql_is_rejected_when_whitelist_is_enabled(self):
        """启用列白名单后空 SQL 必须失败关闭。"""
        # Arrange
        from src.security.permission_check import check_column_whitelist

        # Act
        error = check_column_whitelist("", ["id"])

        # Assert
        assert error is not None
        assert "解析失败" in error

    def test_column_parser_failure_returns_error(self):
        """列白名单解析失败不得默认放行。"""
        # Arrange
        from src.security.permission_check import check_column_whitelist

        # Act
        error = check_column_whitelist("SELECT 'unterminated", ["safe_column"])

        # Assert
        assert error is not None
        assert "解析失败" in error

    def test_row_filter_parser_failure_raises_security_error(self):
        """非法行过滤条件不得回退到原 SQL。"""
        # Arrange
        from src.exceptions import SQLSecurityError
        from src.security.permission_check import inject_row_filter

        # Act / Assert
        with pytest.raises(SQLSecurityError, match="行过滤"):
            inject_row_filter("SELECT * FROM orders", "tenant_id = 'unterminated")


class TestAuditSanitization:
    """覆盖查询审计不记录明文 SQL。"""

    def test_audit_entry_contains_hash_instead_of_sql(self):
        """审计条目必须使用稳定 hash 且不得包含 SQL 原文。"""
        # Arrange
        from src.security.data_masker import build_audit_entry

        sql = "SELECT email FROM users"

        # Act
        entry = build_audit_entry(
            user_id=7,
            tenant_id=3,
            datasource="prod",
            sql=sql,
            row_count=2,
            elapsed_ms=15,
            success=True,
        )

        # Assert
        assert entry["user_id"] == 7
        assert entry["tenant_id"] == 3
        assert len(entry["sql_hash"]) == 64
        assert "sql" not in entry
        assert sql not in str(entry)
