"""报表运行证据、质量说明与持久化回归（23.3、23.5、23.16）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.graph.subgraphs.report_workbench import (
    build_report_workbench_subgraph,
    report_build_relations,
)
from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult
from src.reporting.relations import RelationEvidence

QUALITY_QUESTION = "说明时间覆盖范围、被截断或被权限过滤的部分，以及未能建立的关系"


class TestRuntimeQuality:
    """覆盖 23.3/23.5 运行质量说明基于本轮证据而非独立 Schema 直答。"""

    # 方法作用：验证说明单元延后到其他单元和关系完成后生成，并写回页面。
    # Args: self - 测试实例；quality_first - 是否将说明单元放在查询之前。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize("quality_first", [False, True])
    async def test_quality_uses_finished_results_without_independent_query(self, quality_first: bool) -> None:
        """原始故障问题不得调用独立执行器，页面必须保留样本范围与截断说明。"""
        # Arrange
        plan = ReportPlan(divergence="conservative", requested_datasources=["oracle"], analysis_units=[
            AnalysisUnit(unit_id="sales", question="查询销售趋势"),
            AnalysisUnit(unit_id="quality", question=QUALITY_QUESTION),
        ])
        if quality_first:
            plan.analysis_units.reverse()
        executor = AsyncMock(return_value=UnitResult(unit_id="sales", datasource="oracle", rows=[
            {"year_month": "2026-06", "amount": 10}, {"year_month": "2026-09", "amount": 20},
        ], truncated=True, warnings=["金额为模拟指标"]))

        # Act
        result = await build_report_workbench_subgraph().ainvoke({
            "plan": plan, "execute_unit": executor,
            "authorized_datasources": {"oracle": {}},
        })

        # Assert
        executor.assert_awaited_once()
        quality = next(item for item in result["unit_results"] if item.unit_id == "quality")
        assert quality.answer_kind == "text"
        assert "2026-06" in quality.summary and "2026-09" in quality.summary
        assert "截断" in quality.summary and "金额为模拟指标" in quality.summary
        assert "当前请求未实际执行任何查询" not in quality.summary
        assert "当前查询没有返回数据，无法进行完整趋势分析" not in quality.warnings
        narrative = next(block for section in result["artifact"].document.sections if section.id == "quality" for block in section.blocks if block.type == "narrative")
        assert narrative.content["summary"] == quality.summary
        assert not result["relations"]

    # 方法作用：验证汇总包含运行期追加单元、失败、权限阻断与真实关系结论。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_quality_includes_runtime_units_failures_and_relations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """关系已验证与无法建立必须来自 RelationEvidence，不能从表名猜测。"""
        # Arrange
        quality = AnalysisUnit(unit_id="quality", question=QUALITY_QUESTION)
        units = [AnalysisUnit(unit_id="sales", question="销售"), AnalysisUnit(unit_id="denied", question="财务"), quality]
        runtime = AnalysisUnit(unit_id="runtime", question="补查履约", origin="diverged", rationale="验证趋势", decided_by="runtime")
        relations = [RelationEvidence(left_unit_id="sales", right_unit_id="runtime", evidence_level="verified_relation"), RelationEvidence(left_unit_id="sales", right_unit_id="denied", evidence_level="blocked", limitations=["缺少授权列"])]
        monkeypatch.setattr("src.graph.subgraphs.report_workbench.build_relations", Mock(return_value=relations))
        state = {
            "plan": ReportPlan(requested_datasources=["oracle"], analysis_units=units), "runtime_units": [runtime],
            "unit_results": [UnitResult(unit_id="sales", rows=[{"date": "2026-09-01"}]), UnitResult(unit_id="runtime", rows=[{"date": "2026-09-13"}]), UnitResult(unit_id="denied", status="failed", error_code="DATASOURCE_FORBIDDEN"), UnitResult(unit_id="quality", answer_kind="text", summary="错误的旧直答")],
            "resolved_units": [{"unit_id": "denied", "authorization_status": "denied"}],
        }

        # Act
        result = await report_build_relations(state)

        # Assert
        quality_result = next(item for item in result["unit_results"] if item.unit_id == "quality")
        assert "runtime" in quality_result.summary
        assert "2026-09-13" in quality_result.summary
        assert "权限阻断" in quality_result.summary
        assert "校验通过 1" in quality_result.summary
        assert "缺少授权列" in quality_result.summary
        assert "错误的旧直答" not in quality_result.summary

    # 方法作用：验证零结果不捏造时间范围，也不把缺失权限证据当作没有过滤。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_quality_empty_evidence_is_explicit(self) -> None:
        """只有说明单元时返回证据不足，不返回数据库全量或虚构日期。"""
        # Arrange
        quality = AnalysisUnit(unit_id="quality", question=QUALITY_QUESTION)
        executor = AsyncMock(return_value=UnitResult(unit_id="quality", answer_kind="text"))

        # Act
        result = await build_report_workbench_subgraph().ainvoke({"plan": ReportPlan(divergence="conservative", requested_datasources=["oracle"], analysis_units=[quality]), "execute_unit": executor})

        # Assert
        executor.assert_not_awaited()
        summary = result["unit_results"][0].summary
        assert "没有可用的查询结果样本" in summary
        assert "不能据此认定没有过滤" in summary

    # 方法作用：验证普通 Schema 说明仍使用原执行器且不添加查询无数据告警。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_schema_explanation_remains_direct_answer(self) -> None:
        """不能将所有元数据说明一刀切改成 SQL 或运行汇总。"""
        # Arrange
        unit = AnalysisUnit(unit_id="schema", question="解释订单表字段类型与外键关系")
        executor = AsyncMock(return_value=UnitResult(unit_id="schema", answer_kind="text", summary="字段说明"))

        # Act
        result = await build_report_workbench_subgraph().ainvoke({"plan": ReportPlan(divergence="conservative", analysis_units=[unit]), "execute_unit": executor})

        # Assert
        executor.assert_awaited_once()
        assert result["unit_results"][0].summary == "字段说明"
        assert not result["unit_results"][0].warnings

    # 方法作用：验证执行适配器保留模型已产出的限制而非静默丢弃。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_runner_preserves_analysis_limitations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """样本口径与数据覆盖限制必须传到 UnitResult 和报表质量摘要。"""
        # Arrange
        from src.reporting.runner import ReportRunner
        from src.reporting.store import ReportStore
        app = SimpleNamespace(ainvoke=AsyncMock(return_value={"final_response": {"success": True, "data": [{"amount": 1}], "analysis": {"summary": "结果", "limitations": ["仅返回样本", "金额为模拟指标"]}}}))
        monkeypatch.setattr("src.graph.workflow.app", app)
        unit = AnalysisUnit(unit_id="sales", question="销售")

        # Act
        result = await ReportRunner(store=ReportStore(memory=True))._default_execute_unit(unit, {"plan": ReportPlan(requested_datasources=["oracle"], analysis_units=[unit]), "run_id": "run-test"})

        # Assert
        assert result.warnings == ["仅返回样本", "金额为模拟指标"]

    # 方法作用：验证质量说明不能掩盖所有数据查询失败，且自身权限阻断保持失败。
    # Args: self - 测试实例；denied_quality - 是否仅拒绝说明单元的数据源。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize("denied_quality", [False, True])
    async def test_quality_does_not_hide_execution_failure(self, denied_quality: bool) -> None:
        """运行状态依据真实数据结果和授权错误，不能靠成功的说明文字升级。"""
        # Arrange
        units = [AnalysisUnit(unit_id="sales", question="销售", datasource="oracle"), AnalysisUnit(unit_id="quality", question=QUALITY_QUESTION, datasource="denied" if denied_quality else "oracle")]
        executor = AsyncMock(return_value=UnitResult(unit_id="sales", status="success" if denied_quality else "failed", rows=[{"amount": 1}] if denied_quality else []))

        # Act
        result = await build_report_workbench_subgraph().ainvoke({"plan": ReportPlan(divergence="conservative", requested_datasources=["oracle"], analysis_units=units), "execute_unit": executor, "authorized_datasources": {"oracle": {}}})

        # Assert
        assert result["run_status"] == ("partial" if denied_quality else "failed")
        if denied_quality:
            assert next(item for item in result["unit_results"] if item.unit_id == "quality").error_code == "DATASOURCE_FORBIDDEN"

    # 方法作用：验证仅含日期缺失或异常输入时不制造时间覆盖范围。
    # Args: self - 测试实例；value - 无效时间值。
    # Returns: 无返回值。
    @pytest.mark.parametrize("value", [None, "", "not-a-date", 20260913])
    def test_quality_invalid_dates_remain_unknown(self, value: object) -> None:
        """可解析日期证据不存在时必须说明未知，不把数字当日期。"""
        # Arrange
        from src.reporting.profiling import summarize_runtime_quality
        unit = AnalysisUnit(unit_id="quality", question=QUALITY_QUESTION)

        # Act
        result = summarize_runtime_quality(unit, [UnitResult(unit_id="sales", rows=[{"event_date": value}])], [], [])

        # Assert
        assert "没有可解析的时间列" in result.summary

    # 方法作用：验证质量单元判定不依赖固定编号，也不会劫持实际数据质量查询。
    # Args: self - 测试实例；question - 问题文本；expected - 是否运行说明。
    # Returns: 无返回值。
    @pytest.mark.parametrize(("question", "expected"), [(QUALITY_QUESTION, True), ("披露本报表的时间覆盖与权限过滤", True), ("查询订单缺失率与重复率", False), ("解释订单表字段类型与外键关系", False)])
    def test_quality_question_routing_is_narrow(self, question: str, expected: bool) -> None:
        """只接管报表级多项运行证据说明，其余仍走原查询或元数据分支。"""
        # Arrange
        from src.reporting.profiling import is_runtime_quality_unit
        unit = AnalysisUnit(unit_id="arbitrary_id", question=question)

        # Act
        result = is_runtime_quality_unit(unit)

        # Assert
        assert result is expected
