"""SQL 执行结果上限和脱敏集成测试。"""

from __future__ import annotations

from types import SimpleNamespace


class TestExecuteSQLSecurity:
    """覆盖 4.7 与 12.2/12.3 的执行边界。"""

    # 方法作用：验证 Graph 状态缺少租户字段时仍从可信 ContextVar 传递租户并写入审计。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_execution_uses_context_tenant_when_state_is_legacy(self, monkeypatch):
        """旧 checkpoint 不含 tenant_id 时不得落到租户 0 或默认租户。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.api.auth as auth_module
        import src.security.data_masker as data_masker
        import src.security.sql_execution as sql_execution
        from src.graph.nodes import execute_sql as execute_module

        execution_calls: list[dict] = []

        # 方法作用：记录统一 SQL 执行边界收到的显式租户。
        # Args: args - SQL 执行位置参数；kwargs - 执行选项。
        # Returns: 最小成功结果，避免访问真实数据库。
        async def fake_execute(*args, **kwargs):
            execution_calls.append(dict(kwargs))
            return SimpleNamespace(
                success=True,
                sql=args[0],
                data=[],
                truncated=False,
                details=[],
                error="",
                error_type="",
            )

        audit = AsyncMock()
        monkeypatch.setattr(sql_execution, "validate_and_execute_sql", fake_execute)
        monkeypatch.setattr(data_masker, "log_audit", audit)
        monkeypatch.setattr(execute_module, "get_tenant_policy", lambda: SimpleNamespace(
            datasource_isolation_enabled=False,
        ))
        monkeypatch.setattr(execute_module, "get_settings", lambda: SimpleNamespace(
            max_result_rows=10,
            attribution_max_touchpoint_rows=10,
        ))
        tenant_token = auth_module._current_tenant_id.set(4)  # noqa: SLF001

        # Act
        try:
            await execute_module.execute_sql_node({
                "datasource": "sales",
                "dialect": "postgres",
                "generated_sql": "SELECT 1",
                "request_rate_limit_checked": True,
            })
        finally:
            auth_module._current_tenant_id.reset(tenant_token)  # noqa: SLF001

        # Assert
        assert execution_calls[0]["tenant_id"] == 4
        assert audit.await_args.kwargs["tenant_id"] == 4

    async def test_result_is_bounded_and_masked(self, monkeypatch):
        """查询应有界读取，并在写入 state 前脱敏 PII。"""
        # Arrange
        import sqlalchemy as sa
        from sqlalchemy.ext.asyncio import create_async_engine

        import src.api.auth  # noqa: F401
        import src.config as config_module
        from src.app_context import AppContext, use_app_context
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry
        from src.graph.nodes import execute_sql as execute_module
        from src.security import data_masker

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=sa.pool.StaticPool)
        async with engine.begin() as connection:
            await connection.execute(sa.text(
                "CREATE TABLE pii (id INTEGER, email TEXT, phone TEXT)"
            ))
            await connection.execute(sa.text(
                "INSERT INTO pii VALUES "
                "(1, 'a@example.com', '13812345678'),"
                "(2, 'b@example.com', '13912345678'),"
                "(3, 'c@example.com', '13712345678')"
            ))

        settings = SimpleNamespace(
            max_result_rows=2,
            attribution_max_touchpoint_rows=200,
            max_execution_time=30,
            max_queries_per_hour=100,
            multi_tenant=False,
            database_url="",
            explain_skip_dialects=[],
        )
        monkeypatch.setattr(config_module, "get_settings", lambda: settings)
        monkeypatch.setattr(execute_module, "get_settings", lambda: settings)
        monkeypatch.setattr(data_masker, "get_settings", lambda: settings)
        registry = DataSourceRegistry()
        registry._cache["pii"] = DataSourceConfig(  # noqa: SLF001
            name="pii", dialect="sqlite", mode="embedded", engine=engine,
        )
        context = AppContext(settings)
        context.set_resource("datasource_registry", registry)

        # Act
        with use_app_context(context):
            result = await execute_module.execute_sql_node({
                "datasource": "pii",
                "dialect": "sqlite",
                "generated_sql": "SELECT email, phone FROM pii ORDER BY id",
            })

        # Assert
        assert result["execution_error"] == ""
        assert len(result["query_result_sample"]) == 2
        assert result["query_result_truncated"] is True
        assert result["query_result_sample"][0]["email"] == "a***@example.com"
        assert result["query_result_sample"][0]["phone"] == "138****5678"

        # Arrange：放宽数据库读取上限，但把归因私有候选行限制为两条。
        settings.max_result_rows = 10
        settings.attribution_max_touchpoint_rows = 2

        # Act：归因意图应保留独立候选行，不改变公开样本契约。
        with use_app_context(context):
            attribution_result = await execute_module.execute_sql_node({
                "datasource": "pii",
                "dialect": "sqlite",
                "intent": "attribution",
                "generated_sql": "SELECT id, email FROM pii ORDER BY id",
            })

        # Assert：完整行数用于资格判断，私有候选按专用上限截取。
        assert attribution_result["query_result_full_count"] == 3
        assert len(attribution_result["query_result_sample"]) == 3
        assert len(attribution_result["attribution_candidate_rows"]) == 2

        await engine.dispose()

    async def test_build_response_reports_result_truncation(self):
        """最终响应应报告实际返回行数和结果是否被截断。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        state = {
            "user_query": "查询用户",
            "generated_sql": "SELECT email FROM users",
            "query_result_sample": [{"email": "a***@example.com"}],
            "query_result_full_count": 1,
            "query_result_truncated": True,
            "analysis_result": {},
            "chart_config": {},
        }

        # Act
        result = await build_response_node(state)

        # Assert
        final_response = result["final_response"]
        assert final_response["row_count"] == 1
        assert final_response["truncated"] is True

    async def test_sync_engine_query_runs_without_blocking_path(self, monkeypatch):
        """同步 Engine 也应通过线程池执行并返回结果。"""
        import src.config as config_module
        from src.app_context import AppContext, use_app_context
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry
        from src.graph.nodes import execute_sql as execute_module
        from src.security import data_masker

        class Row:
            _mapping = {"value": 1}

        class Result:
            def fetchmany(self, size):
                return [Row()]

            def close(self):
                return None

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execution_options(self, **kwargs):
                return self

            def execute(self, statement, params=None):
                return Result()

        class Engine:
            def connect(self):
                return Connection()

        settings = SimpleNamespace(
            max_result_rows=10,
            max_execution_time=30,
            max_queries_per_hour=100,
            multi_tenant=False,
            database_url="",
            explain_skip_dialects=[],
        )
        monkeypatch.setattr(config_module, "get_settings", lambda: settings)
        monkeypatch.setattr(execute_module, "get_settings", lambda: settings)
        monkeypatch.setattr(data_masker, "get_settings", lambda: settings)
        registry = DataSourceRegistry()
        registry._cache["oracle"] = DataSourceConfig(  # noqa: SLF001
            name="oracle", dialect="oracle", mode="external", engine=Engine(),
        )
        context = AppContext(settings)
        context.set_resource("datasource_registry", registry)

        with use_app_context(context):
            result = await execute_module.execute_sql_node({
                "datasource": "oracle",
                "dialect": "oracle",
                "generated_sql": "SELECT 1 FROM dual",
            })

        assert result["execution_error"] == ""
        assert result["query_result_sample"] == [{"value": 1}]

    # 方法作用：验证数据源执行异常也会写入失败审计。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_failed_query_is_audited(self, monkeypatch):
        """执行失败不能绕过 query_audit_log。"""
        # Arrange
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.config as config_module
        import src.datasource.registry as registry_module
        from src.graph.nodes import execute_sql as execute_module
        from src.security import data_masker

        settings = SimpleNamespace(
            max_result_rows=10,
            max_execution_time=30,
            max_queries_per_hour=100,
            multi_tenant=False,
            database_url="",
        )
        registry = SimpleNamespace(resolve_or_none=AsyncMock(side_effect=RuntimeError("database unavailable")))
        audit = AsyncMock()
        monkeypatch.setattr(config_module, "get_settings", lambda: settings)
        monkeypatch.setattr(execute_module, "get_settings", lambda: settings)
        monkeypatch.setattr(data_masker, "get_settings", lambda: settings)
        monkeypatch.setattr(data_masker, "log_audit", audit)
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

        # Act
        result = await execute_module.execute_sql_node({
            "datasource": "prod",
            "dialect": "postgres",
            "generated_sql": "SELECT 1",
            "user_id": 7,
            "tenant_id": 3,
            "request_rate_limit_checked": True,
        })

        # Assert
        assert result["execution_error"] == "database unavailable"
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["success"] is False
        assert audit.await_args.kwargs["user_id"] == 7
        assert audit.await_args.kwargs["tenant_id"] == 3
