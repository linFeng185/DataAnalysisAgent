"""报表两阶段发散与档位额度回归，覆盖 23.17。"""

from __future__ import annotations

import pytest


# 方法作用：构造一个含用户单元的计划。
# Args: divergence - 档位；units - 追加的单元；question - 用户单元问题。
# Returns: 通过校验的 ReportPlan。
def _plan(divergence: str = "standard", units: list | None = None, question: str = "生成销售报表"):
    from src.reporting.models import AnalysisUnit, ReportPlan

    return ReportPlan(
        divergence=divergence,
        analysis_units=[AnalysisUnit(question=question), *(units or [])],
    )


# 方法作用：构造一条显著发现，用于触发运行期补查决策。
# Args: significance - 显著性档位。
# Returns: UnitFinding 实例。
def _finding(significance: str = "high"):
    from src.reporting.profiling import FindingItem, UnitFinding

    return UnitFinding(
        unit_id="u1",
        category="anomaly",
        title="异常点",
        method="测试",
        items=[FindingItem(label="第 1 行", value=100.0, significance=significance)],
    )


class TestBudgetTables:
    """验证 23.17：档位额度表与设计一致。"""

    # 方法作用：验证三个档位的额度、轮数与单轮上限。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_budget_tables_match_design(self) -> None:
        """总额度 0/3/6、轮数 0/1/2、单轮 0/2/3。"""
        # Arrange
        from src.reporting.models import (
            divergence_max_rounds,
            divergence_unit_budget,
            divergence_units_per_round,
        )

        # Act / Assert
        assert [divergence_unit_budget(item) for item in ("conservative", "standard", "deep")] == [0, 3, 6]
        assert [divergence_max_rounds(item) for item in ("conservative", "standard", "deep")] == [0, 1, 2]
        assert [divergence_units_per_round(item) for item in ("conservative", "standard", "deep")] == [0, 2, 3]

    # 方法作用：验证未知档位按标准档兜底而不是抛错。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_level_falls_back_to_standard(self) -> None:
        """脏数据不应让计划解析崩溃。"""
        # Arrange
        from src.reporting.models import divergence_unit_budget

        # Act / Assert
        assert divergence_unit_budget("unknown") == 3

    # 方法作用：验证档位进入计划哈希。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_divergence_enters_canonical_hash(self) -> None:
        """同一需求不同档位对应不同执行范围与成本上限。"""
        # Arrange / Act / Assert
        assert _plan("standard").canonical_hash() != _plan("deep").canonical_hash()


class TestUnitInvariants:
    """验证 23.17：单元来源字段的不变量。"""

    # 方法作用：验证用户单元不得携带发散元信息。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_user_unit_forbids_diverged_metadata(self) -> None:
        """来源被误标时必须报错，而不是静默接受。"""
        # Arrange
        from src.reporting.models import AnalysisUnit

        # Act / Assert
        with pytest.raises(ValueError):
            AnalysisUnit(question="销售", origin="user", rationale="不该有")
        with pytest.raises(ValueError):
            AnalysisUnit(question="销售", origin="user", decided_by="plan")

    # 方法作用：验证发散单元必须说明理由与来源阶段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_diverged_unit_requires_rationale_and_decided_by(self) -> None:
        """无法解释的发散单元不允许进入计划。"""
        # Arrange
        from src.reporting.models import AnalysisUnit

        # Act / Assert
        with pytest.raises(ValueError):
            AnalysisUnit(question="退款趋势", origin="diverged")
        with pytest.raises(ValueError):
            AnalysisUnit(question="退款趋势", origin="diverged", rationale="相关")
        ok = AnalysisUnit(question="退款趋势", origin="diverged", rationale="与退款相关", decided_by="plan")
        assert ok.decided_by == "plan"


class TestPlanPhaseDiverge:
    """验证 23.17：计划期发散的服务端约束与降级。"""

    # 方法作用：验证保守档不调用发散模型。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_conservative_skips_llm_call(self, monkeypatch) -> None:
        """保守档保持完全不加料的语义，且不消耗任何模型调用。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        calls: list[str] = []
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: calls.append(task) or True)

        # Act
        plan = await ReportPlanner().diverge(_plan("conservative"))

        # Assert
        assert calls == []
        assert len(plan.analysis_units) == 1

    # 方法作用：验证发散建议被规范化为受控单元。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_suggestions_become_controlled_units(self, monkeypatch) -> None:
        """服务端分配 unit_id、标注来源、固定优先级并继承时间范围。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportDivergeOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportDivergeOutput(proposed_units=[
                {"question": "退款趋势", "datasource": "", "rationale": "与销售额共变", "expected_signal": "负相关"},
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)
        base = _plan("standard")

        # Act
        plan = await ReportPlanner().diverge(base, datasources=["sales"])
        added = [unit for unit in plan.analysis_units if unit.origin == "diverged"]

        # Assert
        assert len(added) == 1
        assert added[0].unit_id not in {unit.unit_id for unit in base.analysis_units}
        assert added[0].decided_by == "plan"
        assert added[0].priority == "secondary"
        assert added[0].rationale == "与销售额共变"
        assert added[0].time_range == base.analysis_units[0].time_range

    # 方法作用：验证含因果措辞的建议被硬校验丢弃。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_causal_rationale_is_dropped(self, monkeypatch) -> None:
        """提示词是软约束，服务端必须硬校验。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportDivergeOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportDivergeOutput(proposed_units=[
                {"question": "退款趋势", "rationale": "退款导致销售额下降"},
                {"question": "履约时效", "rationale": "与客单价共变"},
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)

        # Act
        plan = await ReportPlanner().diverge(_plan("standard"), datasources=["sales"])
        added = [unit.question for unit in plan.analysis_units if unit.origin == "diverged"]

        # Assert
        assert added == ["履约时效"]
        assert any("2" in warning and "1" in warning and "未通过服务端校验" in warning for warning in plan.warnings)

    # 方法作用：验证越权数据源被清空而不是保留。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unauthorized_datasource_is_cleared(self, monkeypatch) -> None:
        """模型在授权集合外的猜测必须清空，由路由重新定位。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportDivergeOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportDivergeOutput(proposed_units=[
                {"question": "退款趋势", "datasource": "hidden_source", "rationale": "相关"},
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)

        # Act
        plan = await ReportPlanner().diverge(_plan("standard"), datasources=["sales"])
        added = [unit for unit in plan.analysis_units if unit.origin == "diverged"]

        # Assert
        assert added[0].datasource == ""

    # 方法作用：验证超出额度的建议被截断。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_suggestions_truncated_to_budget(self, monkeypatch) -> None:
        """标准档总额度 3，模型给 6 条也只能加 3 条。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportDivergeOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportDivergeOutput(proposed_units=[
                {"question": f"补充方向{index}", "rationale": "相关"} for index in range(6)
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)

        # Act
        plan = await ReportPlanner().diverge(_plan("standard"), datasources=["sales"])
        added = [unit for unit in plan.analysis_units if unit.origin == "diverged"]

        # Assert
        assert len(added) == 3

    # 方法作用：验证全局 12 单元上限压缩可用额度。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_global_unit_cap_limits_budget(self, monkeypatch) -> None:
        """用户已有 11 个单元时深度档也只能加 1 个。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.config import get_settings
        from src.llm.output_contracts import ReportDivergeOutput
        from src.reporting.models import AnalysisUnit
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)
        # 上限来自配置（app.yaml），测试显式钉住它，避免随部署配置漂移而失去约束语义。
        monkeypatch.setattr(get_settings(), "report_max_units_per_run", 12)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportDivergeOutput(proposed_units=[
                {"question": f"补充方向{index}", "rationale": "相关"} for index in range(4)
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)
        units = [AnalysisUnit(question=f"用户单元{index}") for index in range(10)]

        # Act
        plan = await ReportPlanner().diverge(_plan("deep", units=units), datasources=["sales"])
        added = [unit for unit in plan.analysis_units if unit.origin == "diverged"]

        # Assert
        assert len(added) == 1
        assert len(plan.analysis_units) == 12

    # 方法作用：验证发散失败只降级留痕，不影响主计划。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_failure_keeps_plan_with_warning(self, monkeypatch) -> None:
        """发散是增强能力，失败不能让报表生成失败。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def boom(prompt_id, sections, **kwargs):
            raise RuntimeError("模型不可用")

        monkeypatch.setattr(llm_invocation, "invoke_structured", boom)
        base = _plan("standard")

        # Act
        plan = await ReportPlanner().diverge(base, datasources=["sales"])

        # Assert
        assert len(plan.analysis_units) == len(base.analysis_units)
        assert any("发散分析不可用" in item for item in plan.warnings)

    # 方法作用：验证无可用模型时静默跳过。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_missing_model_skips_silently(self, monkeypatch) -> None:
        """无模型是配置状态，不是降级。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)
        base = _plan("standard")

        # Act
        plan = await ReportPlanner().diverge(base, datasources=["sales"])

        # Assert
        assert plan.warnings == base.warnings


class TestRuntimeDecision:
    """验证 23.17：运行期回路的确定性前置筛选。"""

    # 方法作用：验证无显著发现时不进入回路。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_significant_finding_skips_loop(self) -> None:
        """深挖没有显著发现说明数据正常，且这条筛选不消耗模型调用。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        state = {"plan": _plan("standard"), "unit_findings": [_finding("low")]}

        # Act / Assert
        assert report_decide_supplement(state) == "continue"

    # 方法作用：验证有显著发现时进入回路。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_significant_finding_enters_loop(self) -> None:
        """显著发现指向尚未覆盖的方向时才补查。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        state = {"plan": _plan("standard"), "unit_findings": [_finding("high")]}

        # Act / Assert
        assert report_decide_supplement(state) == "supplement"

    # 方法作用：验证保守档与额度耗尽时不进入回路。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_conservative_and_exhausted_budget_skip_loop(self) -> None:
        """保守档额度为 0；计划期已用满额度时运行期不得再追加。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement
        from src.reporting.models import AnalysisUnit

        diverged = [
            AnalysisUnit(question=f"计划期发散{index}", origin="diverged", rationale="相关", decided_by="plan")
            for index in range(3)
        ]

        # Act / Assert
        assert report_decide_supplement({"plan": _plan("conservative"), "unit_findings": [_finding()]}) == "continue"
        assert report_decide_supplement({"plan": _plan("standard", units=diverged), "unit_findings": [_finding()]}) == "continue"

    # 方法作用：验证达到轮数上限后停止。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_round_limit_stops_loop(self) -> None:
        """标准档只允许一轮。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        state = {"plan": _plan("standard"), "unit_findings": [_finding()], "divergence_round": 1}

        # Act / Assert
        assert report_decide_supplement(state) == "continue"

    # 方法作用：验证 SQL 额度耗尽时不进入补查模型节点。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_query_budget_exhaustion_skips_supplement(self) -> None:
        """补查必然产生查询；无剩余额度时不得先白耗一次模型调用再被 SQL 边界拒绝。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement
        from src.reporting.run_stats import activate, deactivate

        state = {"plan": _plan("standard"), "unit_findings": [_finding("high")]}
        _stats, token = activate(sql_query_limit=0)
        try:
            # Act
            target = report_decide_supplement(state)
        finally:
            deactivate(token)

        # Assert
        assert target == "continue"


class TestRuntimeSupplement:
    """验证 23.17：运行期补查节点只执行新增单元并并回状态。"""

    # 方法作用：验证补查单元被追加且已有结果不被重跑。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_supplement_appends_without_rerunning_existing(self, monkeypatch) -> None:
        """回路必须只执行新增单元，否则成本成倍放大。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.graph.subgraphs.report_workbench import report_plan_supplement
        from src.llm.output_contracts import ReportSupplementOutput
        from src.reporting.models import UnitResult

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportSupplementOutput(proposed_units=[
                {"question": "退款趋势", "rationale": "解释异常", "supports_finding": "第 1 行异常"},
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)
        executed: list[str] = []

        async def executor(unit, state):
            executed.append(unit.unit_id)
            return UnitResult(unit_id=unit.unit_id, status="success", rows=[{"金额": 1}])

        existing = UnitResult(unit_id="u1", status="success", rows=[{"金额": 2}])
        state = {
            "plan": _plan("standard"),
            "unit_findings": [_finding()],
            "unit_results": [existing],
            "resolved_units": [{"unit_id": "u1", "question": "生成销售报表"}],
            "authorized_datasources": {"sales": {"allowed_columns": ["金额"]}},
            "execute_unit": executor,
            "divergence_round": 0,
        }

        # Act
        update = await report_plan_supplement(state)

        # Assert
        assert len(update["runtime_units"]) == 1
        # 只跑了新增的那个单元，已有单元没有被重跑
        assert executed == [update["runtime_units"][0].unit_id]
        assert len(update["unit_results"]) == 2
        assert update["divergence_round"] == 1
        added = update["resolved_units"][-1]
        assert added["origin"] == "diverged" and added["decided_by"] == "runtime"

    # 方法作用：验证缺少 supports_finding 的建议被丢弃。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_suggestion_without_supports_finding_is_dropped(self, monkeypatch) -> None:
        """无法说明解释哪条发现的建议属于无依据补查。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.graph.subgraphs.report_workbench import report_plan_supplement
        from src.llm.output_contracts import ReportSupplementOutput

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportSupplementOutput(proposed_units=[
                {"question": "无关方向", "rationale": "凑数", "supports_finding": ""},
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)
        state = {
            "plan": _plan("standard"),
            "unit_findings": [_finding()],
            "unit_results": [],
            "resolved_units": [],
            "authorized_datasources": {"sales": {}},
            "divergence_round": 0,
        }

        # Act
        update = await report_plan_supplement(state)

        # Assert
        assert update.get("runtime_units") is None
        assert update["divergence_round"] == 1

    # 方法作用：验证补查模型失败时把降级事实写入已有单元，而不是只记日志。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_supplement_failure_is_disclosed_in_unit_warnings(self, monkeypatch) -> None:
        """运行期补查失败后报表仍发布，但页面必须说明按现有结果继续。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.graph.subgraphs.report_workbench import report_plan_supplement
        from src.reporting.models import UnitResult

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        # 方法作用：模拟补查模型在外部调用边界失败。
        # Args: prompt_id - 提示词标识；sections - 动态分区；kwargs - 其余调用参数。
        # Returns: 不返回，始终抛出瞬态错误。
        async def fail_invoke(prompt_id, sections, **kwargs):
            raise TimeoutError("supplement timeout")

        monkeypatch.setattr(llm_invocation, "invoke_structured", fail_invoke)
        existing = UnitResult(unit_id="u1", status="success", rows=[{"金额": 2}])
        state = {
            "plan": _plan("standard"),
            "unit_findings": [_finding()],
            "unit_results": [existing],
            "resolved_units": [{"unit_id": "u1", "question": "生成销售报表"}],
            "authorized_datasources": {"sales": {"allowed_columns": ["金额"]}},
            "divergence_round": 0,
        }

        # Act
        update = await report_plan_supplement(state)

        # Assert
        assert update.get("runtime_units") is None
        warning = next(item for item in update["unit_results"][0].warnings if "补查" in item)
        assert "未生成可执行单元" in warning
        assert "按现有结果继续" in warning


class TestReservation:
    """验证 23.17：配额必须按最坏情况预占。"""

    # 方法作用：验证预占覆盖运行期回路的上限。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reserve_covers_worst_case_rounds(self) -> None:
        """运行期追加数在预占时无法确定，必须按档位总额度预留。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner

        standard = ReportPlan(divergence="standard", requested_datasources=["a"], analysis_units=[AnalysisUnit(question="销售")])
        conservative = ReportPlan(divergence="conservative", requested_datasources=["a"], analysis_units=[AnalysisUnit(question="销售")])

        # Act
        reserved_standard = ReportRunner._reserve_plan(standard, {standard.analysis_units[0].unit_id: ["a"]})
        reserved_conservative = ReportRunner._reserve_plan(conservative, {conservative.analysis_units[0].unit_id: ["a"]})

        # Assert
        assert reserved_standard.estimated.query_count > reserved_conservative.estimated.query_count
        assert reserved_conservative.estimated.query_count == 1

    # 方法作用：验证预占扣减计划期已用的额度，不把共享额度算两遍。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reserve_subtracts_plan_phase_divergence(self) -> None:
        """深度档额度 6 全部用在计划期时，运行期应预留 0。

        这是真实运行暴露的缺陷：预占按全额 6 × 数据源数预留，把计划期已经
        花掉的额度再算一遍，导致 5 个数据源时凭空多出 30 次查询而误报配额不足。
        """
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.runner import ReportRunner

        user_unit = AnalysisUnit(question="用户需求", unit_id="u1")
        diverged = [
            AnalysisUnit(question=f"计划期发散{index}", unit_id=f"d{index}", origin="diverged", rationale="相关", decided_by="plan")
            for index in range(6)
        ]
        plan = ReportPlan(divergence="deep", requested_datasources=["a"], analysis_units=[user_unit, *diverged])
        routed = {unit.unit_id: ["a"] for unit in plan.analysis_units}

        # Act
        reserved = ReportRunner._reserve_plan(plan, routed)

        # Assert：7 个单元各命中 1 个源，运行期额度为 0，因此恰好等于单元数
        assert reserved.estimated.query_count == len(plan.analysis_units) == 7


class TestPromptVariableContract:
    """验证 23.17：调用方必须提供模板所需的全部变量。

    这是上线后第一次真实运行报出的错：report.diverge 的模板里有 {limit}，
    但调用方把它当 section 传，render 时抛 KeyError 导致整批发散降级。
    只断言「传了哪个 section」抓不到这类问题，必须真的渲染一次。
    """

    # 方法作用：验证发散调用提供的变量足以渲染模板。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_diverge_passes_template_variables(self, monkeypatch) -> None:
        """用调用方实传的 system_values 渲染模板，缺变量会在断言处暴露。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportDivergeOutput
        from src.llm.prompts import get_prompt_definition
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)
        captured: dict = {}

        async def fake_invoke(prompt_id, sections, **kwargs):
            captured["prompt_id"] = prompt_id
            captured["system_values"] = dict(kwargs.get("system_values") or {})
            return ReportDivergeOutput(proposed_units=[])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)

        # Act
        await ReportPlanner().diverge(_plan("standard"), datasources=["sales"])

        # Assert：渲染必须成功（缺变量会在这里抛 ValueError），且额度出现在正文里
        body = get_prompt_definition(captured["prompt_id"]).render(**captured["system_values"])
        assert "3" in body


class TestDivergeUnitTitle:
    """缺口 A：发散单元必须带模型给出的精简标题，不能回退成截断的问题原文。

    真实五库验收产物：14 个区段里 6 个发散单元标题是「按年月、地区、渠道统计订单取消率与取消订…」
    这类截断文本，根因是契约没有 title 字段、解析层也从不赋值。
    """

    # 方法作用：验证发散与补查契约都暴露 title，且边界与 report.plan 单元完全一致。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_diverge_contracts_expose_title(self) -> None:
        """契约没有 title 字段时模型无处可给，页面只能退回截断的问题原文。"""
        # Arrange

        from src.llm.output_contracts import (
            ReportDivergeUnit,
            ReportPlanUnit,
            ReportSupplementUnit,
        )

        # Act / Assert：默认空、12 字可过、13 字被截断到 12 字，三个单元契约逐一对齐。
        # 越界只截断不拒收：展示字段的越界不得让整批建议被 parse_json_model 打回。
        assert ReportDivergeUnit(question="退款趋势").title == ""
        assert ReportSupplementUnit(question="退款趋势").title == ""
        assert ReportPlanUnit(question="销售分析", title="销" * 12).title == "销" * 12
        assert ReportDivergeUnit(question="退款趋势", title="退" * 12).title == "退" * 12
        assert ReportSupplementUnit(question="退款趋势", title="补" * 12).title == "补" * 12
        assert ReportDivergeUnit(question="退款趋势", title="退" * 13).title == "退" * 12
        assert ReportSupplementUnit(question="退款趋势", title="补" * 13).title == "补" * 12
        assert ReportPlanUnit(question="销售分析", title="销" * 13).title == "销" * 12

    # 方法作用：验证两个发散提示词都要求模型给出标题，措辞与 report.plan 一致。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_diverge_prompts_ask_for_unit_title(self) -> None:
        """只加契约不加提示词，模型不会给 title；措辞不一致会让模型按不同标准产出。"""
        # Arrange
        from src.llm.prompts import get_prompt_definition

        # Act
        plan_template = get_prompt_definition("report.plan").template
        templates = {prompt_id: get_prompt_definition(prompt_id).template for prompt_id in ("report.diverge", "report.supplement")}

        # Assert
        for prompt_id, template in templates.items():
            assert '"title"' in template, f"{prompt_id} 的输出示例里没有 title"
            assert "12 个字" in template, f"{prompt_id} 没有字数上限"
            assert "维度罗列" in template, f"{prompt_id} 没有禁止维度罗列开头的措辞"
        assert "不超过 12 个字，概括业务主题" in plan_template
        assert "不超过 12 个字，概括业务主题" in templates["report.diverge"]
        assert "不超过 12 个字，概括业务主题" in templates["report.supplement"]

    # 方法作用：验证发散单元解析层透传标题，且超长标题只收窄不丢单元。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_diverge_unit_title_is_passed_through_and_narrowed(self) -> None:
        """超长标题曾让整个单元通不过 AnalysisUnit 校验被静默丢弃，问题一起消失。"""
        # Arrange
        from src.reporting.models import _UNIT_TITLE_MAX_CHARS
        from src.reporting.planner import ReportPlanner

        raw_units = [
            {"question": "按年月、地区、渠道统计订单取消率与取消订单数", "rationale": "与销售趋势相关", "title": "订单取消率"},
            {"question": "库存不足对履约的影响", "rationale": "与取消率相关", "title": "溢" * 200},
            {"question": "没有标题的补充方向", "rationale": "相关"},
            {"question": "支付方式分布", "rationale": "相关", "title": "  支付   失败分布  "},
        ]

        # Act
        units = ReportPlanner._coerce_diverge_units(
            raw_units,
            allowed_datasources=set(),
            decided_by="plan",
            limit=4,
            reference_units=[],
        )

        # Assert
        assert len(units) == 4, "超长标题不得导致发散单元被整体拒收"
        assert units[0].title == "订单取消率"
        assert units[1].title and len(units[1].title) <= _UNIT_TITLE_MAX_CHARS
        assert units[2].title == ""
        assert units[3].title == "支付 失败分布"
        assert all(unit.decided_by == "plan" for unit in units)
        # 运行期补查走同一个 static 方法（report_workbench 传 decided_by="runtime"），
        # 所以标题透传对两个阶段同时生效，不需要第二处改动。
        runtime_units = ReportPlanner._coerce_diverge_units(
            raw_units,
            allowed_datasources=set(),
            decided_by="runtime",
            limit=4,
            reference_units=[],
        )
        assert [unit.title for unit in runtime_units] == [unit.title for unit in units]
        assert all(unit.decided_by == "runtime" for unit in runtime_units)

    # 方法作用：验证带标题的发散建议一路走到页面区段标题。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_diverge_title_reaches_section_title(self, monkeypatch) -> None:
        """锁住「模型输出 → 计划单元 → 页面区段标题」的完整链路，正是验收里断掉的那一段。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportDivergeOutput
        from src.reporting.document import build_report_document
        from src.reporting.models import UnitResult
        from src.reporting.planner import ReportPlanner

        question = "按年月、地区、渠道统计订单取消率与取消订单数"
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_invoke(prompt_id, sections, **kwargs):
            return ReportDivergeOutput(proposed_units=[
                {"question": question, "title": "订单取消率", "rationale": "与销售趋势相关"},
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)

        # Act
        plan = await ReportPlanner().diverge(_plan("standard"), datasources=["sales"])
        diverged = [unit for unit in plan.analysis_units if unit.origin == "diverged"]
        artifact = build_report_document(plan, [
            UnitResult(
                unit_id=diverged[0].unit_id,
                datasource="sales",
                rows=[{"year_month": "2026-06", "net_sales": 200.0}],
                columns=["year_month", "net_sales"],
            ),
        ])

        # Assert
        assert diverged and diverged[0].title == "订单取消率"
        section = next(item for item in artifact.document.sections if item.id == diverged[0].unit_id)
        assert section.title == "订单取消率"
        assert not section.title.endswith("…")
        assert section.title != question[:20]
