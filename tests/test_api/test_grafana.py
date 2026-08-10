"""Grafana SimpleJSON 查询协议测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestGrafanaRoutes:
    """覆盖 Grafana 协议功能 19.8。"""

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

        # Act
        result = await route.search()

        # Assert
        assert result == ["sales"]

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
