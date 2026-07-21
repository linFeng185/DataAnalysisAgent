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

    # 方法作用：验证未显式传入连接池时审计函数仍使用全局 PG 池持久化。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_log_audit_uses_global_pool_by_default(self, monkeypatch):
        """生产调用不得因调用方省略 pg_pool 而退化为普通日志。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.config as config_module
        import src.memory.pg_pool as pool_module
        from src.security import data_masker

        pool = SimpleNamespace(execute=AsyncMock())
        get_pool = AsyncMock(return_value=pool)
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://audit:test@db/app",
        ))
        monkeypatch.setattr(pool_module, "get_pg_pool", get_pool)

        # Act
        await data_masker.log_audit(
            user_id=7,
            tenant_id=3,
            datasource="prod",
            sql="SELECT secret FROM users",
            row_count=0,
            elapsed_ms=12,
            success=False,
            error_message="permission denied",
        )

        # Assert
        get_pool.assert_awaited_once()
        pool.execute.assert_awaited_once()
        assert "SELECT secret" not in str(pool.execute.await_args)


class TestDatasourceAuthorization:
    """覆盖数据源候选发现和显式访问授权。"""

    # 方法作用：验证自动发现只返回当前用户有权访问的数据源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_discovery_returns_only_authorized_datasources(self, monkeypatch):
        """未显式选择数据源时，私有和租户权限必须先于模型选择生效。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        import src.security.permission_check as permission_module

        connection = SimpleNamespace(fetch=AsyncMock(return_value=[
            {
                "datasource_name": "sales",
                "owner_user_id": 7,
                "visibility": "private",
                "access_level": "read",
                "allowed_columns": ["order_id", "amount"],
                "row_filter_sql": "org_id = 9",
            },
            {
                "datasource_name": "finance",
                "owner_user_id": 8,
                "visibility": "private",
                "access_level": "read",
                "allowed_columns": [],
                "row_filter_sql": "",
            },
            {
                "datasource_name": "warehouse",
                "owner_user_id": 8,
                "visibility": "tenant",
                "access_level": "read",
                "allowed_columns": ["sku"],
                "row_filter_sql": "tenant_id = 4",
            },
        ]))
        acquire = MagicMock()
        acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
        acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        pool = SimpleNamespace(acquire=acquire)
        monkeypatch.setattr(permission_module, "get_pg_pool", AsyncMock(return_value=pool), raising=False)

        # Act
        result = await permission_module.resolve_datasource_access(
            [
                {"name": "sales", "description": "销售订单"},
                {"name": "finance", "description": "财务"},
                {"name": "warehouse", "description": "库存"},
            ],
            [],
            tenant_id=4,
            user_id=7,
            role="analyst",
            multi_tenant=True,
        )

        # Assert
        assert list(result) == ["sales", "warehouse"]
        assert result["sales"]["allowed_columns"] == ["order_id", "amount"]
        assert result["warehouse"]["row_filter_sql"] == "tenant_id = 4"

    # 方法作用：验证显式选择无权数据源时失败关闭。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explicit_unauthorized_datasource_is_rejected(self, monkeypatch):
        """显式请求存在但无权限的数据源必须返回授权错误。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        import src.security.permission_check as permission_module

        connection = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        acquire = MagicMock()
        acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
        acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        pool = SimpleNamespace(acquire=acquire)
        monkeypatch.setattr(permission_module, "get_pg_pool", AsyncMock(return_value=pool), raising=False)

        # Act / Assert
        with pytest.raises(PermissionError, match="无权访问数据源"):
            await permission_module.resolve_datasource_access(
                [{"name": "finance", "description": "财务"}],
                ["finance"],
                tenant_id=4,
                user_id=7,
                role="analyst",
                multi_tenant=True,
            )

    # 方法作用：验证权限存储异常时多租户访问失败关闭。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_permission_store_failure_is_closed(self, monkeypatch):
        """权限数据库不可用时不得回退为全数据源访问。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.security.permission_check as permission_module

        monkeypatch.setattr(
            permission_module,
            "get_pg_pool",
            AsyncMock(side_effect=RuntimeError("permission db down")),
            raising=False,
        )

        # Act / Assert
        with pytest.raises(PermissionError, match="权限服务不可用"):
            await permission_module.resolve_datasource_access(
                [{"name": "sales", "description": "销售"}],
                [],
                tenant_id=4,
                user_id=7,
                role="analyst",
                multi_tenant=True,
            )
