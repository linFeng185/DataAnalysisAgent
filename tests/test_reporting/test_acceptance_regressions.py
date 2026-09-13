"""真实深度报表暴露的年份和查询复用回归（23.1、23.3）。"""

from unittest.mock import AsyncMock, Mock

import pytest

from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult
from src.reporting.planner import ReportPlanner


class TestReportAcceptanceRegressions:
    """覆盖 23.1 年度范围与 23.3 不同问题隔离。"""

    # 方法作用：验证全局年度在模型拆分、规则回退及发散继承中不丢失。
    # Args: self - 测试实例；fail_model - 是否模拟规划模型不可用。
    # Returns: 无返回值。
    @pytest.mark.parametrize("fail_model", [False, True])
    async def test_report_year_survives_unit_planning(self, fail_model: bool) -> None:
        """2026 年经营报表不得退回默认最近 90 天。"""
        # Arrange
        model = AsyncMock()
        model.ainvoke.return_value = {"analysis_units": [{"unit_id": "sales", "question": "销售趋势"}]}
        if fail_model:
            model.ainvoke.side_effect = TimeoutError("不可用")

        # Act
        plan = await ReportPlanner(model).parse("立即生成《2026 年经营全景报表》，不要定时，页面保留7天：1. 销售趋势", datasources=["oracle"], divergence="deep")

        # Assert
        assert all(unit.time_range.kind == "absolute" for unit in plan.analysis_units)
        assert all(unit.time_range.value == "2026-01-01/2026-12-31" for unit in plan.analysis_units)
        assert ReportPlanner._inherit_time_range(plan.analysis_units).value == "2026-01-01/2026-12-31"
        assert plan.divergence == "deep" and plan.page_ttl_days == 7

    # 方法作用：验证同维度指标但业务问题不同的深度单元必须各自执行。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_distinct_questions_do_not_reuse_query(self) -> None:
        """退货、库存、广告问题不能因 dimensions/metrics 为空复用同一结果。"""
        # Arrange
        plan = ReportPlan(divergence="conservative", requested_datasources=["oracle"], analysis_units=[AnalysisUnit(unit_id="returns", question="统计退款规模"), AnalysisUnit(unit_id="inventory", question="统计库存周转天数")])
        executor = AsyncMock(return_value=UnitResult(unit_id="returns", rows=[{"amount": 1}]))

        # Act
        result = await build_report_workbench_subgraph().ainvoke({"plan": plan, "execute_unit": executor})

        # Assert
        assert executor.await_count == 2
        assert len(result["unit_results"]) == 2

    # 方法作用：验证年度质量说明不残留上一轮默认的最近 90 天口径。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_annual_quality_does_not_claim_ninety_days(self) -> None:
        """年度范围质量说明必须对应用户确认的全年，而非固定天数。"""
        # Arrange
        from src.reporting.models import TimeRange
        from src.reporting.profiling import summarize_runtime_quality
        unit = AnalysisUnit(question="说明时间覆盖和截断", time_range=TimeRange(kind="absolute", value="2026-01-01/2026-12-31"))

        # Act
        result = summarize_runtime_quality(unit, [], [], [])

        # Assert
        assert "最近 90 天" not in result.summary

    # 方法作用：验证禁止旧补查标记穿过真实 LangGraph 状态通道后仍控制路由。
    # Args: self - 测试实例；monkeypatch - 补丁工具；suppress - 是否禁用旧补查。
    # Returns: 无返回值。
    @pytest.mark.parametrize("suppress", [False, True])
    async def test_supplement_guard_survives_compiled_state(self, monkeypatch: pytest.MonkeyPatch, suppress: bool) -> None:
        """仅 Mock 执行器检查入参不够，未声明字段会被编译图入口丢弃。"""
        # Arrange
        from langgraph.graph import END, StateGraph

        from src.graph.state import AnalysisState
        from src.graph.workflow import after_analyze_result
        monkeypatch.setattr("src.graph.nodes.supplemental_query.build_supplemental_request", Mock(return_value={"question": "补查缺失证据"}))
        graph = StateGraph(AnalysisState)

        # 方法作用：将实际图状态经过原路由后记录到返回字段。
        # Args: state - 编译图过滤后的真实状态。
        # Returns: 保存路由名称的状态更新。
        async def route_probe(state: AnalysisState) -> dict:
            return {"input_explanation": after_analyze_result(state)}

        graph.add_node("probe", route_probe)
        graph.set_entry_point("probe")
        graph.add_edge("probe", END)

        # Act
        result = await graph.compile().ainvoke({"suppress_supplemental_query": suppress, "intent": "query", "analysis_result": {"evidence_status": "supplementable"}})

        # Assert
        assert result["input_explanation"] == ("generate_chart" if suppress else "supplemental_query")
