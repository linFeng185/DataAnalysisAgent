"""实时钻取的受控 SQL 与脱敏测试，覆盖 23.12。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlglot


class TestLiveDrilldown:
    """覆盖 23.12 三方言、SQL 注入、授权撤销和结果预算。"""

    # 方法作用：构造只含服务端允许列和参数的签发定义。
    # Args: self - 测试实例。
    # Returns: 实时钻取定义。
    def definition(self) -> dict:
        """客户端不能提供查询或数据源。"""
        return {"drilldown_id": "live", "mode": "live", "columns": ["region", "amount"], "allowed_filters": [{"field": "region", "operator": "eq"}], "max_rows": 100}

    # 方法作用：验证过滤值始终作为字面量进入三种方言的 AST。
    # Args: self - 测试实例；dialect - 数据库方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "clickhouse"])
    def test_live_query_uses_literal_filters(self, dialect: str) -> None:
        """单引号和注入文本不会变成查询结构。"""
        from src.reporting.live_drilldown import build_live_sql

        sql = build_live_sql("SELECT region, amount FROM sales", dialect, self.definition(), {"region": "' OR 1=1 --"}, 1, 20)
        tree = sqlglot.parse_one(sql, read=dialect)

        assert len(sqlglot.parse(sql, read=dialect)) == 1
        assert tree.args["limit"].expression.this == "21"
        assert any(literal.this == "' OR 1=1 --" for literal in tree.find_all(sqlglot.exp.Literal))

    # 方法作用：验证非法字段、分页预算、非只读模板和多语句均被拒绝。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_live_query_rejects_unissued_and_unbounded(self) -> None:
        """查询模板和过滤参数都不能跨越服务端边界。"""
        from src.reporting.live_drilldown import build_live_sql

        for query in ("DELETE FROM sales", "SELECT region FROM sales; SELECT amount FROM private"):
            with pytest.raises(ValueError):
                build_live_sql(query, "postgres", self.definition(), {}, 1, 20)
        with pytest.raises(ValueError):
            build_live_sql("SELECT region, amount FROM sales", "postgres", self.definition(), {"secret": 1}, 1, 20)
        with pytest.raises(ValueError):
            build_live_sql("SELECT region, amount FROM sales", "postgres", self.definition(), {}, 100, 100)

    # 方法作用：验证重新授权后执行只读查询，脱敏且只返回已签发列。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_live_execution_applies_current_acl_and_masking(self, monkeypatch) -> None:
        import src.security.sql_execution as execution
        from src.reporting.live_drilldown import execute_live_drilldown

        executor = AsyncMock(return_value=SimpleNamespace(success=True, data=[{"region": "east", "amount": "13812345678", "secret": "private"}], truncated=False))
        monkeypatch.setattr(execution, "validate_and_execute_sql", executor)
        query = {"sql": "SELECT region, amount FROM sales", "datasource": "sales", "dialect": "postgres"}
        permission = {"allowed_columns": ["region", "amount"], "row_filter_sql": "department_id = 3", "dialect": "postgres"}

        result = await execute_live_drilldown(query, permission, self.definition(), {}, 1, 20, tenant_id=3)

        assert "department_id = 3" in executor.call_args.args[0]
        assert executor.call_args.kwargs["tenant_id"] == 3
        assert executor.call_args.kwargs["max_rows"] == 21
        assert "secret" not in str(result)
        assert "13812345678" not in str(result)
        assert result["mode"] == "live"

    # 方法作用：验证列权限收紧时，不执行任何旧报表 SQL。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_live_execution_denies_revoked_column(self, monkeypatch) -> None:
        import src.security.sql_execution as execution
        from src.reporting.live_drilldown import execute_live_drilldown

        executor = AsyncMock()
        monkeypatch.setattr(execution, "validate_and_execute_sql", executor)
        query = {"sql": "SELECT region, amount FROM sales", "datasource": "sales", "dialect": "postgres"}

        with pytest.raises(PermissionError):
            await execute_live_drilldown(query, {"allowed_columns": ["region"]}, self.definition(), {}, 1, 20, tenant_id=3)

        executor.assert_not_awaited()
