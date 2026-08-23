"""Grafana SimpleJSON 查询协议测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestGrafanaRoutes:
    """覆盖 Grafana 协议功能 19.8。"""

    # 方法作用：验证 Grafana OpenAPI 明确说明时间宏的使用约定。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_query_openapi_documents_time_filter_macro(self) -> None:
        """range 不会自动改写 SQL，文档必须要求显式宏。"""
        # Arrange
        import src.api.routes.grafana as route

        # Act
        operation = next(
            item for item in route.router.routes
            if getattr(item, "path", "") == "/grafana/query"
        )

        # Assert
        assert "$__timeFilter(column)" in str(getattr(operation, "description", ""))

    # 方法作用：验证 search 只返回当前身份授权的数据源名称。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_search_returns_authorized_datasources(self, monkeypatch) -> None:
        """数据源搜索应使用统一 ACL 解析。"""
        # Arrange
        import src.api.routes.grafana as route
        from src.security.tenant_policy import RequestIdentity

        monkeypatch.setattr(route, "get_current_identity", lambda: RequestIdentity(4, 7, "analyst"))
        monkeypatch.setattr(route, "get_registry", lambda: SimpleNamespace(list_all=AsyncMock(return_value=[
            {"name": "sales", "dialect": "postgres"},
        ])))
        monkeypatch.setattr(route, "resolve_datasource_access", AsyncMock(return_value={
            "sales": {"name": "sales", "dialect": "postgres"},
        }))
        policy = object()
        monkeypatch.setattr(route, "get_tenant_policy", lambda: policy, raising=False)

        # Act
        result = await route.search()

        # Assert
        assert result == ["sales"]
        assert route.resolve_datasource_access.await_args.kwargs["tenant_policy"] is policy

    # 方法作用：验证 Grafana 数据源列表失败时不以无租户调用覆盖原始 TypeError。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_available_datasources_does_not_retry_internal_type_error(self, monkeypatch) -> None:
        """内部列表错误应转换为 503，且 Registry 只执行一次。"""
        # Arrange
        import pytest
        from fastapi import HTTPException

        import src.api.routes.grafana as route
        from src.security.tenant_policy import RequestIdentity

        calls = 0

        async def _list_all(*, tenant_id: int | None = None):
            nonlocal calls
            calls += 1
            assert tenant_id == 4
            raise TypeError("registry body failure")

        monkeypatch.setattr(route, "get_registry", lambda: SimpleNamespace(list_all=_list_all))

        # Act / Assert
        with pytest.raises(HTTPException) as exc_info:
            await route._available_datasources(RequestIdentity(4, 7, "analyst"))
        assert exc_info.value.status_code == 503
        assert calls == 1

    # 方法作用：验证 query 返回 Grafana table 结构并复用只读有界执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_query_returns_table_and_blocks_write_sql(self, monkeypatch) -> None:
        """查询成功返回列行数据，写 SQL 返回 HTTP 422。"""
        # Arrange
        import pytest
        from fastapi import HTTPException

        import src.api.routes.grafana as route
        from src.security.sql_execution import SQLExecutionResult
        from src.security.tenant_policy import RequestIdentity

        identity = RequestIdentity(4, 7, "analyst")
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_registry", lambda: SimpleNamespace(list_all=AsyncMock(return_value=[
            {"name": "sales", "dialect": "postgres"},
        ])))
        monkeypatch.setattr(route, "resolve_datasource_access", AsyncMock(return_value={
            "sales": {"name": "sales", "dialect": "postgres", "allowed_columns": [], "row_filter_sql": ""},
        }))
        monkeypatch.setattr(route, "validate_and_execute_sql", AsyncMock(return_value=SQLExecutionResult(
            success=True, sql="SELECT id, value FROM metrics", dialect="postgres",
            data=[{"id": 1, "value": 2}], row_count=1,
        )))

        # Act
        result = await route.query({"targets": [{"refId": "A", "datasource": "sales", "rawSql": "SELECT id, value FROM metrics"}]})

        # Assert
        assert result[0]["type"] == "table"
        assert result[0]["columns"] == [{"text": "id"}, {"text": "value"}]
        assert result[0]["rows"] == [[1, 2]]
        assert route.validate_and_execute_sql.await_args.kwargs["tenant_id"] == 4

        monkeypatch.setattr(route, "validate_sql", lambda sql, dialect: SimpleNamespace(valid=False, errors=[{"message": "禁止: DROP"}]))
        with pytest.raises(HTTPException) as exc_info:
            await route.query({"targets": [{"refId": "A", "datasource": "sales", "rawSql": "DROP TABLE metrics"}]})
        assert exc_info.value.status_code == 422

    # 方法作用：验证 Grafana target 指向未授权数据源时在执行前返回 403。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_query_rejects_unauthorized_datasource(self, monkeypatch) -> None:
        """数据源权限错误不得被转换为空结果或执行请求。"""
        # Arrange
        import pytest
        from fastapi import HTTPException

        import src.api.routes.grafana as route
        from src.security.tenant_policy import RequestIdentity

        monkeypatch.setattr(route, "get_current_identity", lambda: RequestIdentity(4, 7, "analyst"))
        monkeypatch.setattr(
            route,
            "get_registry",
            lambda: SimpleNamespace(list_all=AsyncMock(return_value=[])),
        )
        monkeypatch.setattr(route, "resolve_datasource_access", AsyncMock(side_effect=PermissionError("denied")))

        # Act
        with pytest.raises(HTTPException) as exc_info:
            await route.query({"targets": [{"refId": "A", "datasource": "foreign", "rawSql": "SELECT 1"}]})

        # Assert
        assert exc_info.value.status_code == 403

    # 方法作用：验证多目标查询并发执行且单目标故障不会丢失成功结果。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_multi_target_query_is_concurrent_and_failure_isolated(self, monkeypatch) -> None:
        """一个目标失败时应返回错误表，其余目标仍正常完成。"""
        # Arrange
        import src.api.routes.grafana as route
        from src.security.sql_execution import SQLExecutionResult
        from src.security.tenant_policy import RequestIdentity

        active = 0
        max_active = 0

        # 方法作用：模拟可观察并发的数据库执行，并让指定 SQL 返回后端失败。
        # Args: sql - 已校验 SQL；args/kwargs - 兼容统一执行器参数。
        # Returns: 成功或失败的统一执行结果。
        async def _execute(sql, *args, **kwargs):
            nonlocal active, max_active
            del args, kwargs
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            if "broken" in sql:
                return SQLExecutionResult(success=False, sql=sql, dialect="postgres", error="backend unavailable", error_type="database")
            return SQLExecutionResult(success=True, sql=sql, dialect="postgres", data=[{"value": 1}], row_count=1)

        identity = RequestIdentity(4, 7, "analyst")
        available = [{"name": "sales", "dialect": "postgres"}]
        permission = {"name": "sales", "dialect": "postgres", "allowed_columns": [], "row_filter_sql": ""}
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_available_datasources", AsyncMock(return_value=available))
        monkeypatch.setattr(route, "resolve_datasource_access", AsyncMock(return_value={"sales": permission}))
        monkeypatch.setattr(route, "validate_and_execute_sql", _execute)

        # Act
        result = await route.query({"targets": [
            {"refId": "A", "datasource": "sales", "rawSql": "SELECT 1 AS value"},
            {"refId": "B", "datasource": "sales", "rawSql": "SELECT broken FROM metrics"},
        ]})

        # Assert
        assert max_active == 2
        assert result[0]["rows"] == [[1]]
        assert result[1]["error"] == "backend unavailable"

    # 方法作用：验证单目标超过执行时间预算时返回 HTTP 504。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_single_target_timeout_returns_504(self, monkeypatch) -> None:
        """超时任务必须被取消并暴露明确网关超时状态。"""
        # Arrange
        import pytest
        from fastapi import HTTPException

        import src.api.routes.grafana as route
        from src.security.tenant_policy import RequestIdentity

        # 方法作用：模拟超过请求时间预算的数据库调用。
        # Args: args/kwargs - 兼容统一执行器参数。
        # Returns: 不返回，等待期间由 timeout 取消。
        async def _slow_execute(*args, **kwargs):
            del args, kwargs
            await asyncio.sleep(1)

        identity = RequestIdentity(4, 7, "analyst")
        available = [{"name": "sales", "dialect": "postgres"}]
        permission = {"name": "sales", "dialect": "postgres", "allowed_columns": [], "row_filter_sql": ""}
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_available_datasources", AsyncMock(return_value=available))
        monkeypatch.setattr(route, "resolve_datasource_access", AsyncMock(return_value={"sales": permission}))
        monkeypatch.setattr(route, "validate_and_execute_sql", _slow_execute)
        monkeypatch.setattr(route, "_TARGET_TIMEOUT_SECONDS", 0.001)

        # Act / Assert
        with pytest.raises(HTTPException) as exc_info:
            await route.query({"targets": [
                {"refId": "A", "datasource": "sales", "rawSql": "SELECT 1"},
            ]})
        assert exc_info.value.status_code == 504
