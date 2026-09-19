# tests/test_reporting/test_live_drilldown_issuance.py
"""实时钻取模板签发边界：只有单源且模板可解析时才允许签发 live 钻取。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlglot


# 方法作用：构造只返回固定结果的工作流替身，避免真实 LangGraph 与数据库依赖。
# Args: response - ainvoke 返回的结果字典。
# Returns: 提供 await ainvoke 的假工作流对象。
def _fake_workflow(response: dict):
    class FakeApp:
        # 方法作用：忽略入参直接返回固定结果。
        # Args: payload - 工作流状态；config - 线程配置。
        # Returns: 预置结果字典。
        async def ainvoke(self, payload, config):
            return response

    return FakeApp()


# 方法作用：用线上同一个拼接函数产出多源合并 SQL，避免测试写死一种会漂移的格式。
# Args: 无。
# Returns: 含两段来源头（-- oracle / -- ch）的合并 SQL 文本。
def _merged_multi_source_sql() -> str:
    from src.graph.nodes.multi_source import _format_source_sql

    return _format_source_sql([
        {"datasource": "oracle", "success": True, "sql": "SELECT sales FROM orders"},
        {"datasource": "ch", "success": True, "sql": "SELECT attributed FROM attribution"},
    ])


# 方法作用：构造运行器执行单元所需的报表状态。
# Args: plan - 报表计划；run_id - 运行标识；unit - 分析单元；sources - 路由命中的数据源。
# Returns: 可直接传给 _default_execute_unit 的状态字典。
def _state(plan, run_id: str, unit, sources: list[str]) -> dict:
    return {
        "plan": plan,
        "run_id": run_id,
        "report_id": f"rep-{run_id}",
        "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
        "authorized_datasources": {name: {} for name in sources},
        "unit_datasources": {str(unit.unit_id): list(sources)} if len(sources) > 1 else {},
    }


_OWNER = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")


class TestLiveTemplateIssuance:
    """覆盖运行器只对单源且可解析的模板签发实时钻取。"""

    # 方法作用：验证多源单元的合并 SQL 既不入库模板，也不签发出 live 钻取定义。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_multi_source_unit_issues_no_live_drilldown(self, monkeypatch) -> None:
        """合并 SQL 含两段来源头时必然解析失败：不得存坏模板，也不得签发坏链接。"""
        import src.graph.workflow as workflow_module
        from src.reporting.document import build_report_document
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        merged_sql = _merged_multi_source_sql()
        assert "-- oracle" in merged_sql and "-- ch" in merged_sql
        monkeypatch.setattr(workflow_module, "app", _fake_workflow({
            "final_response": {"success": True, "data": [{"sales": 12, "attributed": 18}]},
            "generated_sql": merged_sql,
            "dialect": "mysql",
        }))
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(question="跨源销售额对比")
        plan = ReportPlan(requested_datasources=["oracle", "ch"], analysis_units=[unit])

        result = await runner._default_execute_unit(unit, _state(plan, "run-live-multi", unit, ["oracle", "ch"]))

        # Assert 1：多源单元不签发 live 钻取，也不留下不可解析的模板。
        assert result.status == "success"
        assert result.live_query_id == ""
        assert store._live_queries == {}
        # Assert 2：快照 SQL 证据必须保留，不能为了修钻取把合并 SQL 砍掉。
        snapshots = await store.list_unit_snapshots("run-live-multi", _OWNER)
        assert "-- oracle" in snapshots[0]["sql_text"] and "-- ch" in snapshots[0]["sql_text"]
        # Assert 3：页面里不再出现 live 钻取，快照钻取仍然覆盖明细查看。
        artifact = build_report_document(plan, [result])
        assert artifact.drilldowns, "多源单元仍应保留 snapshot 钻取"
        assert all(item["mode"] == "snapshot" for item in artifact.drilldowns)

    # 方法作用：验证单源单元照旧签发实时钻取，防止修多源时把单源一起关掉。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_single_source_unit_still_issues_live_drilldown(self, monkeypatch) -> None:
        """单源 SQL 可解析时模板入库，页面照旧签发 live 钻取定义。"""
        import src.graph.workflow as workflow_module
        from src.reporting.document import build_report_document
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        sql = "SELECT region, amount FROM sales"
        monkeypatch.setattr(workflow_module, "app", _fake_workflow({
            "final_response": {"success": True, "data": [{"region": "east", "amount": 3}]},
            "generated_sql": sql,
            "dialect": "mysql",
        }))
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(question="销售明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])

        result = await runner._default_execute_unit(unit, _state(plan, "run-live-single", unit, ["oracle"]))

        assert result.status == "success"
        assert result.live_query_id, "单源单元必须照旧签发实时钻取模板"
        stored = store._live_queries[result.live_query_id]
        assert stored["sql"] == sql
        assert len(sqlglot.parse(stored["sql"], read=stored["dialect"])) == 1
        artifact = build_report_document(plan, [result])
        live = [item for item in artifact.drilldowns if item["mode"] == "live"]
        assert len(live) == 1
        assert live[0]["query_ref"] == result.live_query_id

    # 方法作用：验证单源单元但 SQL 为多语句时同样跳过签发，不产生坏链接。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_single_source_multi_statement_sql_skips_issuance(self, monkeypatch) -> None:
        """模板可解析性由 SQL 本身决定，不能只看数据源个数。"""
        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        monkeypatch.setattr(workflow_module, "app", _fake_workflow({
            "final_response": {"success": True, "data": [{"region": "east"}]},
            "generated_sql": "SELECT region FROM sales; SELECT amount FROM private",
            "dialect": "mysql",
        }))
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(question="销售明细")
        plan = ReportPlan(requested_datasources=["oracle"], analysis_units=[unit])

        result = await runner._default_execute_unit(unit, _state(plan, "run-live-multi-stmt", unit, ["oracle"]))

        assert result.live_query_id == ""
        assert store._live_queries == {}


class TestTemplateGuard:
    """覆盖模板可解析性预检与 ParseError 的类型转换。"""

    # 方法作用：验证预检只接受单条可解析查询，且解析失败不抛异常。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_is_single_query_judges_template_shape(self) -> None:
        from src.reporting.live_drilldown import is_single_query

        assert is_single_query(_merged_multi_source_sql(), "mysql") is False
        assert is_single_query("SELECT region, amount FROM sales", "postgres") is True
        assert is_single_query("SELECT region FROM sales; SELECT amount FROM private", "postgres") is False
        assert is_single_query("", "mysql") is False

    # 方法作用：验证不可解析模板转成 ValueError，接口层才能按 4xx 而不是 500 处理。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_build_live_sql_converts_parse_error_to_value_error(self) -> None:
        """sqlglot.errors.ParseError 不是 ValueError，不转换就会冒泡成 500。"""
        from src.reporting.live_drilldown import build_live_sql

        definition = {"drilldown_id": "live", "columns": ["sales"], "allowed_filters": [], "max_rows": 100}
        with pytest.raises(ValueError) as failure:
            build_live_sql(_merged_multi_source_sql(), "mysql", definition, {}, 1, 20)

        assert "模板" in str(failure.value)
        assert "sqlglot" not in str(failure.value)
