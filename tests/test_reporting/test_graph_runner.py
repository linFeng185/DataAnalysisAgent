"""报表 LangGraph 子图和运行器测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class TestReportWorkbenchSubgraph:
    """覆盖功能 23.3 基础、23.10：多分析单元和失败隔离。"""

    # 方法作用：验证同一数据源的多个分析单元可独立执行并生成动态页面。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_subgraph_isolates_unit_failure(self) -> None:
        """一个单元失败时其他单元仍发布 partial 页面和可见 callout。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[AnalysisUnit(unit_id="sales", question="销售", metrics=["销售额"]), AnalysisUnit(unit_id="bad", question="库存")],
        )

        async def execute(unit, _state):
            if unit.unit_id == "bad":
                return UnitResult(unit_id=unit.unit_id, datasource="sales", status="failed", error_code="QUERY_FAILED", user_error_message="该单元查询失败")
            return UnitResult(unit_id=unit.unit_id, datasource="sales", rows=[{"month": "1", "value": 3}, {"month": "2", "value": 5}])

        graph = build_report_workbench_subgraph()

        # Act
        result = await graph.ainvoke({"plan": plan, "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"}, "execute_unit": execute})

        # Assert
        assert result["run_status"] == "partial"
        assert len(result["unit_results"]) == 2
        assert result["artifact"].document.permissions["drilldown"] is True
        assert any(block.type == "callout" for section in result["artifact"].document.sections for block in section.blocks)


class TestReportRunner:
    """覆盖功能 23.10：运行状态、节点日志和用户可读错误。"""

    # 方法作用：验证运行器创建运行、写步骤日志并持久化成功版本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_runner_persists_successful_version(self) -> None:
        """成功运行应返回 report_id、版本和可访问页面。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        store = ReportStore()
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        plan = ReportPlan(requested_datasources=["sales"], analysis_units=[AnalysisUnit(question="销售")])
        job = await store.create_job(identity, "生成销售", plan, "runner-1")

        async def execute(_unit, _state):
            return UnitResult(unit_id="unit_1", datasource="sales", rows=[{"name": "A", "value": 1}])

        runner = ReportRunner(store=store, execute_unit=execute)

        # Act
        result = await runner.run_job(job.id, identity)

        # Assert
        assert result["status"] == "succeeded"
        assert result["report_id"]
        assert result["version"] == 1
        assert (await store.list_jobs(identity))[0].status == "succeeded"
