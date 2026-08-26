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

    # 方法作用：验证子图在数据源授权映射明确时阻断越权分析单元。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_subgraph_blocks_unauthorized_datasource(self) -> None:
        """未授权数据源不得调用执行器，运行结果应为失败并返回可读错误。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
        from src.reporting.models import AnalysisUnit, ReportPlan

        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[AnalysisUnit(unit_id="crm", question="客户分析", datasource="crm")],
        )
        calls: list[str] = []

        async def execute(unit, _state):
            calls.append(unit.unit_id)
            raise AssertionError("未授权单元不应进入执行器")

        # Act
        result = await build_report_workbench_subgraph().ainvoke({
            "plan": plan,
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "authorized_datasources": {"sales": {"allowed_columns": []}},
            "execute_unit": execute,
        })

        # Assert
        assert calls == []
        assert result["run_status"] == "failed"
        assert result["unit_results"][0].error_code == "DATASOURCE_FORBIDDEN"

    # 方法作用：验证相同取数粒度只执行一次，同时为每个业务分析单元保留结果区段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_subgraph_deduplicates_query_without_dropping_units(self) -> None:
        """重复查询应合并执行，但页面仍需包含两个分析单元。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[
                AnalysisUnit(unit_id="sales", question="销售额", dimensions=["month"], metrics=["amount"]),
                AnalysisUnit(unit_id="orders", question="订单数", dimensions=["month"], metrics=["amount"]),
            ],
        )
        calls: list[str] = []

        async def execute(unit, _state):
            calls.append(unit.unit_id)
            return UnitResult(unit_id=unit.unit_id, datasource="sales", rows=[{"month": "1", "amount": 10}])

        # Act
        result = await build_report_workbench_subgraph().ainvoke({"plan": plan, "execute_unit": execute})

        # Assert
        assert calls == ["sales"]
        assert {item.unit_id for item in result["unit_results"]} == {"sales", "orders"}
        assert {section.id for section in result["artifact"].document.sections} >= {"sales", "orders"}

    # 方法作用：验证不同时间窗口不会被错误合并为同一条取数查询。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_subgraph_keeps_distinct_time_ranges_separate(self) -> None:
        """同一数据源和粒度但时间范围不同，必须分别执行。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
        from src.reporting.models import AnalysisUnit, ReportPlan, TimeRange, UnitResult

        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[
                AnalysisUnit(unit_id="recent", question="近 7 天销售", dimensions=["month"], metrics=["amount"], time_range=TimeRange(value="7d")),
                AnalysisUnit(unit_id="quarter", question="近 90 天销售", dimensions=["month"], metrics=["amount"], time_range=TimeRange(value="90d")),
            ],
        )
        calls: list[str] = []

        async def execute(unit, _state):
            calls.append(unit.unit_id)
            return UnitResult(unit_id=unit.unit_id, datasource="sales", rows=[{"month": "1", "amount": 10}])

        # Act
        result = await build_report_workbench_subgraph().ainvoke({"plan": plan, "execute_unit": execute})

        # Assert
        assert calls == ["recent", "quarter"]
        assert {item.unit_id for item in result["unit_results"]} == {"recent", "quarter"}


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
