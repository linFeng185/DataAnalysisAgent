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

    # 方法作用：验证 PostgreSQL 与 SQL Server 的 SELECT INTO 写表语法被阻断。
    # Args: self - pytest 测试类实例；dialect - SQL 方言。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("dialect", ["postgres", "mssql"])
    def test_select_into_is_rejected(self, dialect):
        """SELECT 外观不得掩盖 INTO 创建表副作用。"""
        logger.debug("test_select_into_is_rejected 入口", extra={"dialect": dialect})
        try:
            # Arrange
            from src.graph.nodes.layer3_validate import validate_readonly_sql

            # Act
            errors = validate_readonly_sql(
                "SELECT * INTO backup_orders FROM orders",
                dialect,
            )

            # Assert
            assert errors
            assert errors[0]["type"] == "security_block"
            logger.info("test_select_into_is_rejected 完成", extra={"dialect": dialect})
        except Exception as exc:
            logger.error("test_select_into_is_rejected 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证 PostgreSQL 具有状态副作用的序列函数被阻断。
    # Args: self - pytest 测试类实例；function_sql - 待验证函数表达式。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "function_sql",
        ["setval('order_id_seq', 1, false)", "nextval('order_id_seq')"],
    )
    def test_postgres_state_mutating_functions_are_rejected(self, function_sql):
        """可在 SELECT 中改变数据库状态的函数不得执行。"""
        logger.debug(
            "test_postgres_state_mutating_functions_are_rejected 入口",
            extra={"function_sql": function_sql},
        )
        try:
            # Arrange
            from src.graph.nodes.layer3_validate import validate_readonly_sql

            # Act
            errors = validate_readonly_sql(f"SELECT {function_sql}", "postgres")

            # Assert
            assert errors
            assert errors[0]["type"] == "security_block"
            logger.info("test_postgres_state_mutating_functions_are_rejected 完成")
        except Exception as exc:
            logger.error(
                "test_postgres_state_mutating_functions_are_rejected 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证顶层 SELECT 中嵌套的数据修改 CTE 仍被识别为写操作。
    # Args: self - pytest 测试类实例；cte_sql - 含写操作的 PostgreSQL CTE。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "cte_sql",
        [
            "WITH changed AS (DELETE FROM users RETURNING id) SELECT * FROM changed",
            "WITH changed AS (INSERT INTO users(id) VALUES (1) RETURNING id) SELECT * FROM changed",
            "WITH changed AS (UPDATE users SET name = 'x' RETURNING id) SELECT * FROM changed",
        ],
    )
    def test_data_modifying_cte_is_rejected(self, cte_sql: str) -> None:
        """根节点是 SELECT 也不能掩盖 CTE 内部的数据修改。"""
        # Arrange
        from src.security.sql_execution import validate_sql

        # Act
        result = validate_sql(cte_sql, "postgres")

        # Assert
        assert result.valid is False
        assert result.errors[0]["type"] == "security_block"

    # 方法作用：验证可从 SELECT 触发数据库状态、文件或外联副作用的函数被拒绝。
    # Args: self - pytest 测试类实例；dialect - SQL 方言；sql - 副作用查询。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("postgres", "SELECT pg_write_file('/tmp/x', 'data', false)"),
            ("postgres", "SELECT pg_copy_file('/tmp/source', '/tmp/target')"),
            ("postgres", "SELECT pg_unlink('/tmp/x')"),
            ("postgres", "SELECT lo_import('/tmp/source')"),
            ("postgres", "SELECT pg_notify('events', 'payload')"),
            ("mysql", "SELECT GET_LOCK('analysis-lock', 10)"),
            ("oracle", "SELECT UTL_INADDR.GET_HOST_ADDRESS('internal.example') FROM dual"),
            ("mssql", "SELECT * FROM OPENQUERY(linked_server, 'SELECT 1')"),
        ],
    )
    def test_select_side_effect_functions_are_rejected(self, dialect: str, sql: str) -> None:
        """只读白名单必须阻断文件访问、锁、通知和数据库外联函数。"""
        # Arrange
        from src.security.sql_execution import validate_sql

        # Act
        result = validate_sql(sql, dialect)

        # Assert
        assert result.valid is False
        assert result.errors[0]["type"] == "security_block"

    # 方法作用：验证统一 SQL 边界在 AST 解析前执行可配置字符预算。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_length_limit_covers_direct_sql_entrypoints(self, monkeypatch) -> None:
        """Grafana、自动化和 Tool 直达 SQL 不能绕过 SQL 字符预算。"""
        # Arrange
        from types import SimpleNamespace

        import src.config as config_module
        from src.security.sql_execution import validate_sql

        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(max_sql_chars=8),
        )

        # Act
        boundary = validate_sql("SELECT 1", "postgres")
        oversized = validate_sql("SELECT 10", "postgres")

        # Assert
        assert boundary.valid is True
        assert oversized.valid is False
        assert oversized.errors[0]["type"] == "security_block"
        assert "8" in oversized.errors[0]["message"]

    # 方法作用：验证 EXPLAIN 估算扫描行数超出预算时不会执行真实查询。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_scan_budget_blocks_execution_before_query(self, monkeypatch) -> None:
        """已知超大扫描计划必须在 `execute_bounded()` 前失败关闭。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.config as config_module
        import src.security.sql_execution as execution_module

        connector = SimpleNamespace(
            explain=AsyncMock(return_value={
                "valid": True,
                "errors": [],
                "estimated_scan_rows": 101,
            }),
            execute_bounded=AsyncMock(return_value=([], False)),
        )
        monkeypatch.setattr(
            execution_module,
            "_resolve_sql_target",
            AsyncMock(return_value=(object(), connector, "postgres")),
        )
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(
                max_sql_chars=20_000,
                max_execution_time=30,
                max_scan_rows=100,
                max_result_rows=10,
            ),
        )

        # Act
        result = await execution_module.validate_and_execute_sql(
            "SELECT id FROM large_table",
            "sales",
            "postgres",
        )

        # Assert
        assert result.success is False
        assert result.error_type == "resource_limit"
        connector.execute_bounded.assert_not_awaited()

    # 方法作用：验证数据库驱动未及时返回时统一执行边界按配置终止等待。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_execution_timeout_covers_connectors_without_native_timeout(self, monkeypatch) -> None:
        """Oracle、SQL Server 和 SQLite 等路径也必须受总超时保护。"""
        # Arrange
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.config as config_module
        import src.security.sql_execution as execution_module

        # 方法作用：模拟无法在配置时间内完成的数据库查询。
        # Args: sql - SQL；max_rows - 最大返回行数。
        # Returns: 不返回，等待期间由统一超时取消。
        async def slow_execute(sql: str, max_rows: int):
            await asyncio.sleep(0.1)
            return [], False

        connector = SimpleNamespace(execute_bounded=AsyncMock(side_effect=slow_execute))
        monkeypatch.setattr(
            execution_module,
            "_resolve_sql_target",
            AsyncMock(return_value=(object(), connector, "sqlite")),
        )
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(
                max_sql_chars=20_000,
                max_execution_time=0.01,
                max_scan_rows=100,
                max_result_rows=10,
            ),
        )

        # Act
        result = await execution_module.validate_and_execute_sql(
            "SELECT id FROM records",
            "local",
            "sqlite",
            explain=False,
        )

        # Assert
        assert result.success is False
        assert result.error_type == "timeout"
        assert result.error == "数据库查询超时"


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

    # 方法作用：验证 UNION 的每个物理表读取分支都会注入行过滤。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_row_filter_is_injected_into_every_union_branch(self) -> None:
        """攻击者不能把未过滤查询放进 UNION 第一分支绕过租户隔离。"""
        # Arrange
        from src.security.permission_check import inject_row_filter

        sql = "SELECT id FROM orders UNION ALL SELECT id FROM archived_orders"

        # Act
        filtered = inject_row_filter(sql, "tenant_id = 9")

        # Assert
        assert filtered.upper().count("TENANT_ID = 9") == 2

    # 方法作用：验证限定表名的列白名单不会错误放行同名敏感列。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_qualified_column_whitelist_distinguishes_same_named_columns(self) -> None:
        """只允许 public.id 时，secret.id 必须被识别为越权列。"""
        # Arrange
        from src.security.permission_check import check_column_whitelist

        # Act
        allowed_error = check_column_whitelist("SELECT public.id FROM public", ["public.id"])
        denied_error = check_column_whitelist("SELECT secret.id FROM secret", ["public.id"])

        # Assert
        assert allowed_error is None
        assert denied_error is not None
        assert "secret.id" in denied_error

    # 方法作用：验证 SQL 表别名不能冒充列白名单中的授权真实表名。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_qualified_whitelist_resolves_alias_to_physical_table(self) -> None:
        """把 secret 表别名设为 public 时仍应按 secret.id 判定权限。"""
        # Arrange
        from src.security.permission_check import check_column_whitelist

        # Act
        error = check_column_whitelist(
            "SELECT public.id FROM secret AS public",
            ["public.id"],
        )

        # Assert
        assert error is not None
        assert "secret.id" in error

    # 方法作用：验证函数参数中的整行通配符不能绕过列白名单。
    # Args: self - pytest 测试类实例；sql - 会返回整行内容的查询。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT ROW(orders.*)::text FROM orders",
            "SELECT JSON_BUILD_ARRAY(orders.*) FROM orders",
            "SELECT CONCAT(orders.*) FROM orders",
        ],
    )
    def test_whole_row_wildcard_inside_function_is_rejected(self, sql: str) -> None:
        """通配符藏在函数或行构造器中时仍会泄露未授权列。"""
        # Arrange
        from src.security.permission_check import check_column_whitelist

        # Act
        error = check_column_whitelist(sql, ["orders.id"])

        # Assert
        assert error is not None
        assert "通配符" in error


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

    # 方法作用：验证调用方省略兼容参数时仍使用 AppContext 的集中租户策略。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_default_policy_comes_from_app_context(self, monkeypatch) -> None:
        """多租户 Context 下不得因漏传 tenant_policy 自动进入 trusted 模式。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        import src.app_context as app_context_module
        import src.security.permission_check as permission_module
        from src.security.tenant_policy import TenantPolicy

        connection = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        acquire = MagicMock()
        acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
        acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            permission_module,
            "get_pg_pool",
            AsyncMock(return_value=SimpleNamespace(acquire=acquire)),
        )
        monkeypatch.setattr(
            app_context_module,
            "get_tenant_policy",
            lambda: TenantPolicy(multi_tenant=True),
        )

        # Act / Assert
        with pytest.raises(PermissionError, match="无权访问数据源: finance"):
            await permission_module.resolve_datasource_access(
                [{"name": "finance", "dialect": "postgres"}],
                ["finance"],
                tenant_id=4,
                user_id=7,
                role="analyst",
            )

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
