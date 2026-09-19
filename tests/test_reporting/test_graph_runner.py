"""报表 LangGraph 子图和运行器测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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
                AnalysisUnit(unit_id="orders", question="销售额", dimensions=["month"], metrics=["amount"]),
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

    # 方法作用：验证查询规划不重复裁剪已经通过上游计划门禁的单元。
    # Args: self - pytest 测试类实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_query_planning_does_not_silently_retrim_resolved_units(self, monkeypatch) -> None:
        """运行期配置变化也不能让 resolved_units 在无披露情况下整段消失。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_plan_queries
        from src.reporting.models import AnalysisUnit, ReportPlan

        plan = ReportPlan(analysis_units=[
            AnalysisUnit(unit_id="sales", question="统计销售额"),
            AnalysisUnit(unit_id="orders", question="统计订单量"),
        ])
        resolved = [
            {"unit_id": "sales", "question": "统计销售额", "datasource": "db", "dimensions": [], "metrics": []},
            {"unit_id": "orders", "question": "统计订单量", "datasource": "db", "dimensions": [], "metrics": []},
        ]
        monkeypatch.setattr("src.reporting.models.max_plan_units", lambda: 1)

        # Act
        result = await report_plan_queries({"plan": plan, "resolved_units": resolved})

        # Assert
        assert [item["unit_id"] for item in result["query_plans"]] == ["sales", "orders"]

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

    # 方法作用：验证零行结果按数据类与说明类分别映射为 partial 和 success。
    # Args: self - pytest 测试类实例；monkeypatch - 依赖替换工具；source - 响应来源；expected_status - 期望状态；expected_error - 期望错误码。
    # Returns: 无返回值。
    @pytest.mark.parametrize(("source", "expected_status", "expected_error"), [
        ("sql_query", "partial", "NO_DATA"),
        ("llm_direct", "success", ""),
    ])
    @pytest.mark.asyncio
    async def test_empty_result_status_depends_on_answer_kind(
        self,
        monkeypatch,
        source: str,
        expected_status: str,
        expected_error: str,
    ) -> None:
        """数据类空结果如实披露，说明类空结果保持成功。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.graph.workflow as workflow_module
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        graph = SimpleNamespace(ainvoke=AsyncMock(return_value={
            "final_response": {"success": True, "status": "success", "source": source, "data": []},
            "generated_sql": "SELECT amount FROM sales WHERE 1 = 0",
            "dialect": "postgres",
            "query_result_full_count": 0,
        }))
        monkeypatch.setattr(workflow_module, "app", graph)
        monkeypatch.setattr(
            "src.graph.node_routing.decide_node_sequence",
            AsyncMock(return_value=[]),
        )
        store = ReportStore(memory=True)
        runner = ReportRunner(store=store)
        unit = AnalysisUnit(unit_id="empty", question="统计销售额", datasource="sales")
        plan = ReportPlan(requested_datasources=["sales"], analysis_units=[unit])
        state = {
            "plan": plan,
            "run_id": "run-empty",
            "report_id": "report-empty",
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "authorized_datasources": {"sales": {"dialect": "postgres"}},
        }

        # Act
        result = await runner._default_execute_unit(unit, state)

        # Assert
        assert result.status == expected_status
        assert result.error_code == expected_error
        assert result.rows == []
        assert any("暂无数据" in warning for warning in result.warnings) is (source == "sql_query")

    # 方法作用：验证运行器把真实预占查询数写入本轮 SQL 硬预算。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_runner_configures_sql_budget_from_reserved_plan(self, monkeypatch) -> None:
        """运行期硬控必须使用 `_reserve_plan()` 的真实扇出值，而非解析期旧估算。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.reporting.run_stats as run_stats_module
        from src.reporting.models import AnalysisUnit, EstimatedResources, ReportPlan, UnitResult
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[AnalysisUnit(unit_id="sales", question="销售")],
            divergence="conservative",
            estimated=EstimatedResources(query_count=9),
        )
        job = await store.create_job(identity, "生成销售", plan, "runner-budget-1")
        configured: list[int] = []
        original_configure = run_stats_module.configure_sql_query_limit

        # 方法作用：记录运行器下发的 SQL 预算并调用真实配置函数。
        # Args: limit - 本轮预占查询数。
        # Returns: 无返回值。
        def capture_limit(limit: int) -> None:
            configured.append(limit)
            original_configure(limit)

        monkeypatch.setattr(run_stats_module, "configure_sql_query_limit", capture_limit)
        runner = ReportRunner(
            store=store,
            execute_unit=AsyncMock(return_value=UnitResult(unit_id="sales", datasource="sales", rows=[{"value": 1}])),
        )
        monkeypatch.setattr(runner, "_route_units", AsyncMock(return_value={"sales": ["sales"]}))

        # Act
        result = await runner.run_job(job.id, identity)

        # Assert
        assert result["status"] == "succeeded"
        assert configured == [1]


class TestSqlBudgetRetryGates:
    """覆盖 SQL 额度耗尽后的上卷、换源与瞬态重试门禁。"""

    # 方法作用：验证样本上卷前先检查剩余 SQL 额度。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_oversample_retry_stops_when_sql_budget_is_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """初次查询用完额度后，应保留已取得的截断结果且不再启动一次模型工作流。"""
        # Arrange
        from unittest.mock import AsyncMock

        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult
        from src.reporting.run_stats import activate, deactivate, try_reserve_sql_query
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        runner = ReportRunner(store=ReportStore(memory=True))
        monkeypatch.setattr(runner, "_persist_unit_snapshot", AsyncMock())
        calls: list[str] = []

        # 方法作用：模拟首次工作流消耗最后一次 SQL 额度并返回被截断结果。
        # Args: unit - 当前单元；_state - 运行状态；trace - 快照追踪数据。
        # Returns: 首次查询的截断结果。
        async def execute_workflow(unit: Any, _state: dict[str, Any], trace: dict[str, Any]) -> UnitResult:
            calls.append(unit.question)
            assert try_reserve_sql_query() is True
            trace.update({"truncated": True, "full_count": 6000, "row_count": 1})
            return UnitResult(unit_id=unit.unit_id, datasource="sales", rows=[{"value": 1}], truncated=True)

        monkeypatch.setattr(runner, "_execute_unit_workflow", execute_workflow)
        unit = AnalysisUnit(unit_id="sales", question="销售趋势", datasource="sales")
        state = {"plan": ReportPlan(requested_datasources=["sales"], analysis_units=[unit])}
        _stats, token = activate(sql_query_limit=1)
        try:
            # Act
            result = await runner._default_execute_unit(unit, state)
        finally:
            deactivate(token)

        # Assert
        assert calls == ["销售趋势"]
        assert result.rows == [{"value": 1}]
        assert result.truncated is True

    # 方法作用：验证换源重试前先检查剩余 SQL 额度。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_datasource_fallback_stops_when_sql_budget_is_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """首个数据源用完额度且失败后，不应再调用模型为候选源生成注定无法执行的 SQL。"""
        # Arrange
        from unittest.mock import AsyncMock

        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult
        from src.reporting.run_stats import activate, deactivate, try_reserve_sql_query
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore

        runner = ReportRunner(store=ReportStore(memory=True))
        monkeypatch.setattr(runner, "_persist_unit_snapshot", AsyncMock())
        calls: list[str] = []

        # 方法作用：模拟首个数据源消耗最后一次 SQL 额度后返回失败。
        # Args: unit - 当前单元；_state - 运行状态；_trace - 快照追踪数据。
        # Returns: 数据源执行失败结果。
        async def execute_workflow(unit: Any, _state: dict[str, Any], _trace: dict[str, Any]) -> UnitResult:
            calls.append(str(unit.datasource))
            assert try_reserve_sql_query() is True
            return UnitResult(
                unit_id=unit.unit_id,
                datasource=str(unit.datasource),
                status="failed",
                error_code="TRANSIENT_DATABASE",
                user_error_message="数据源暂时不可用",
            )

        monkeypatch.setattr(runner, "_execute_unit_workflow", execute_workflow)
        unit = AnalysisUnit(unit_id="sales", question="销售趋势", datasource="primary")
        plan = ReportPlan(requested_datasources=["primary", "backup"], analysis_units=[unit])
        state = {
            "plan": plan,
            "authorized_datasources": {"primary": {}, "backup": {}},
        }
        _stats, token = activate(sql_query_limit=1)
        try:
            # Act
            result = await runner._default_execute_unit(unit, state)
        finally:
            deactivate(token)

        # Assert
        assert calls == ["primary"]
        assert result.status == "failed"
        assert result.datasource == "primary"

    # 方法作用：验证子图瞬态结果重试前检查剩余 SQL 额度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_transient_result_retry_stops_when_sql_budget_is_exhausted(self) -> None:
        """执行器已用完最后一次额度时，子图不得再次进入包含 LLM 的完整单元执行器。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import _execute_single_unit
        from src.reporting.models import AnalysisUnit, UnitResult
        from src.reporting.run_stats import activate, deactivate, try_reserve_sql_query

        unit = AnalysisUnit(unit_id="sales", question="销售趋势", datasource="sales")
        calls = 0

        # 方法作用：模拟首次执行消耗最后一次 SQL 额度并返回瞬态失败。
        # Args: _unit - 当前单元；_state - 运行状态。
        # Returns: 首次为瞬态失败；若被错误重试则返回成功以暴露多余调用。
        async def executor(_unit: Any, _state: dict[str, Any]) -> UnitResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                assert try_reserve_sql_query() is True
                return UnitResult(
                    unit_id=unit.unit_id,
                    datasource="sales",
                    status="failed",
                    error_code="TRANSIENT_DATABASE",
                    user_error_message="连接暂时不可用",
                )
            return UnitResult(unit_id=unit.unit_id, datasource="sales", rows=[{"value": 1}])

        _stats, token = activate(sql_query_limit=1)
        try:
            # Act
            result = await _execute_single_unit({}, unit, "sales", {}, executor)
        finally:
            deactivate(token)

        # Assert
        assert calls == 1
        assert result.status == "failed"
        assert result.error_code == "TRANSIENT_DATABASE"

    # 方法作用：验证执行异常后的重试也受剩余 SQL 额度约束。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_exception_retry_stops_when_sql_budget_is_exhausted(self) -> None:
        """SQL 尝试后抛出的异常不能绕过瞬态结果分支继续白耗模型调用。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import _execute_single_unit
        from src.reporting.models import AnalysisUnit
        from src.reporting.run_stats import activate, deactivate, try_reserve_sql_query

        unit = AnalysisUnit(unit_id="sales", question="销售趋势", datasource="sales")
        calls = 0

        # 方法作用：模拟 SQL 尝试消耗最后一次额度后在执行边界抛错。
        # Args: _unit - 当前单元；_state - 运行状态。
        # Returns: 不返回，始终抛出瞬态异常。
        async def executor(_unit: Any, _state: dict[str, Any]) -> None:
            nonlocal calls
            calls += 1
            assert try_reserve_sql_query() is True
            raise TimeoutError("temporary")

        _stats, token = activate(sql_query_limit=1)
        try:
            # Act
            result = await _execute_single_unit({}, unit, "sales", {}, executor)
        finally:
            deactivate(token)

        # Assert
        assert calls == 1
        assert result.status == "failed"
        assert result.error_code == "UNIT_EXECUTION_FAILED"


class TestDirectAnswerUnitOutcome:
    """覆盖直答类单元的状态判定。

    回归 2026-09-13：data_quality 这类元数据说明单元走 llm_direct，产出是文字结论、
    没有数据行。而成功单元的判据是「status 为 success 且有数据行」，于是它被算成
    未完成，整份报表被判为 partial（expected 12 / success 11）。
    """

    # 方法作用：直答单元无数据行时，运行状态仍应为 succeeded。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_direct_answer_unit_keeps_run_succeeded(self) -> None:
        """说明类单元的结论在摘要文字里，无数据行不等于失败。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[
                AnalysisUnit(unit_id="data", question="销售趋势", metrics=["销售额"]),
                AnalysisUnit(unit_id="meta", question="说明时间覆盖范围"),
            ],
        )

        # 方法作用：数据单元返回数据行，说明类单元只返回文字结论。
        # Args: unit - 待执行单元；_state - 子图状态。
        # Returns: 对应的单元结果。
        async def execute(unit, _state):
            if unit.unit_id == "meta":
                return UnitResult(
                    unit_id=unit.unit_id,
                    datasource="sales",
                    answer_kind="text",
                    summary="当前 Schema 的时间覆盖范围如下…",
                )
            return UnitResult(unit_id=unit.unit_id, datasource="sales", rows=[{"month": "1", "value": 3}])

        graph = build_report_workbench_subgraph()

        # Act
        result = await graph.ainvoke(
            {"plan": plan, "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"}, "execute_unit": execute}
        )

        # Assert
        assert result["run_status"] == "succeeded"

    # 方法作用：说明类单元的 callout 文案不得写成「暂无数据」。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_direct_answer_callout_labels_as_explanation(self) -> None:
        """说明类单元要在页面上标明结论在摘要里，而不是像缺数据。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[AnalysisUnit(unit_id="meta", question="说明时间覆盖范围")],
        )

        # 方法作用：返回说明类单元结果。
        # Args: unit - 待执行单元；_state - 子图状态。
        # Returns: 仅含文字结论的单元结果。
        async def execute(unit, _state):
            return UnitResult(
                unit_id=unit.unit_id,
                datasource="sales",
                answer_kind="text",
                summary="时间覆盖范围说明…",
            )

        graph = build_report_workbench_subgraph()

        # Act
        result = await graph.ainvoke(
            {"plan": plan, "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"}, "execute_unit": execute}
        )

        # Assert
        callouts = [
            block
            for section in result["artifact"].document.sections
            for block in section.blocks
            if block.type == "callout"
        ]
        assert callouts and callouts[0].title == "该单元为说明类分析"
