"""缺同比/环比基期时的独立补查触发契约（覆盖 23.16 与本计划 Task 4）。

事故背景：报表只拿回单月数据时恰好产不出任何发现，运行期发散回路的
「至少一条 high/moderate 发现」门槛因此永不满足，回路不触发、同比永远为 null。
缺基期本身必须能独立触发补查，不能依赖「已经发现了什么」。
"""

from __future__ import annotations

from typing import Any

import pytest


# 方法作用：构造含指定分析单元的计划，供决策函数直接调用。
# Args: divergence - 发散档位；units - 计划分析单元；为空时给一个要求同比的单元。
# Returns: 通过校验的 ReportPlan。
def _plan(divergence: str = "standard", units: list[Any] | None = None):
    from src.reporting.models import AnalysisUnit, ReportPlan

    return ReportPlan(
        divergence=divergence,
        requested_datasources=["sales"],
        analysis_units=list(units) if units else [AnalysisUnit(unit_id="sales", question="按年月统计净销售额并给出同比")],
    )


# 方法作用：把样本行包装成已执行单元结果。
# Args: unit_id - 单元标识；rows - 本单元实际取回的样本行。
# Returns: UnitResult 实例。
def _result(unit_id: str, rows: list[dict[str, Any]]):
    from src.reporting.models import UnitResult

    return UnitResult(unit_id=unit_id, rows=rows)


# 方法作用：构造一条显著发现，用于验证既有触发路径未被削弱。
# Args: significance - 显著性档位。
# Returns: UnitFinding 实例。
def _finding(significance: str = "high"):
    from src.reporting.profiling import FindingItem, UnitFinding

    return UnitFinding(
        unit_id="sales",
        category="anomaly",
        title="异常点",
        method="测试",
        items=[FindingItem(label="2026-06", value=100.0, significance=significance)],
    )


# 方法作用：连续生成从起始年月起的月份标签。
# Args: year - 起始年；month - 起始月；count - 月份数。
# Returns: "YYYY-MM" 标签列表。
def _month_labels(year: int, month: int, count: int) -> list[str]:
    labels: list[str] = []
    for _ in range(count):
        labels.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return labels


# 方法作用：构造按月粒度的样本行。
# Args: labels - "YYYY-MM" 标签序列。
# Returns: 结果行列表。
def _monthly_rows(labels: list[str]) -> list[dict[str, Any]]:
    return [{"year_month": label, "net_sales": 100 + index} for index, label in enumerate(labels)]


# 方法作用：构造从起始日起连续日粒度的样本行。
# Args: start - 起始日（ISO 文本）；days - 天数（含起始日）。
# Returns: 结果行列表。
def _daily_rows(start: str, days: int) -> list[dict[str, Any]]:
    from datetime import date, timedelta

    first = date.fromisoformat(start)
    return [
        {"stat_date": (first + timedelta(days=offset)).isoformat(), "net_sales": 100 + offset % 7}
        for offset in range(days)
    ]


# 方法作用：按真实派生路径调用缺基期判定，列与指标都从单元结果同源取。
# Args: unit - 分析单元；rows - 样本行；findings - 已有发现；metrics - 该单元已过授权过滤的指标列。
# Returns: 补查理由；无需补查返回 None。
def _judge(unit: Any, rows: list[dict[str, Any]], findings: list[Any] | None = None, *, metrics: list[str] | None = None):
    from src.reporting.models import UnitResult
    from src.reporting.profiling import needs_baseline_supplement, profile_inputs

    result = UnitResult(unit_id="sales", rows=list(rows))
    columns, declared_metrics = profile_inputs(result, list(metrics if metrics is not None else ["net_sales"]))
    return needs_baseline_supplement(unit, list(rows), list(findings or []), columns=columns, metrics=declared_metrics)


class TestBaselineSupplement:
    """覆盖「数据残缺也要能补查」的确定性判定。"""

    # 方法作用：验证只有单月数据但要求同比时触发补查。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_single_month_with_yoy_request_triggers_supplement(self) -> None:
        """事故：单月数据产不出发现，回路门槛因此永不满足。"""
        # Arrange

        rows = [{"year_month": "2026-06", "net_sales": 100}]
        unit = {"question": "按年月统计净销售额并给出同比"}

        # Act
        reason = _judge(unit, rows)

        # Assert：理由必须给用户写明缺什么基数、当前跨度是多少
        assert reason and "同比" in reason
        assert "2026-06" in reason and "1 个月" in reason

    # 方法作用：验证跨度足够时不补查。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sufficient_span_does_not_supplement(self) -> None:
        """13 个周期、首尾相隔 548 天，已过产出方的 390 天门槛，基期确实在样本里。"""
        # Arrange

        rows = [{"year_month": f"2026-{month:02d}", "net_sales": 100 + month} for month in range(1, 13)]
        rows.append({"year_month": "2025-06", "net_sales": 90})

        # Act / Assert
        assert _judge({"question": "按年月统计净销售额并给出同比"}, rows) is None

    # 方法作用：验证未要求同环比时不补查。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_baseline_request_does_not_supplement(self) -> None:
        """普通取数需求不得被当成缺基期。"""
        # Arrange

        rows = [{"year_month": "2026-06", "net_sales": 100}]

        # Act / Assert
        assert _judge({"question": "列出本月净销售额"}, rows) is None

    # 方法作用：验证 12 个含基期月份仍判为不足。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_twelve_month_span_still_missing_yoy_baseline(self) -> None:
        """12 个月（334 天）离一个完整年度还差一截，判定与产出方都会卡住。"""
        # Arrange

        rows = [{"year_month": f"2025-{month:02d}", "net_sales": 100} for month in range(7, 13)]
        rows += [{"year_month": f"2026-{month:02d}", "net_sales": 100} for month in range(1, 7)]

        # Act
        reason = _judge({"question": "按年月统计净销售额并给出同比"}, rows)

        # Assert
        assert reason and "同比" in reason

    # 方法作用：验证 13 个日历月存在去年同期时不再误判为缺基期。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_thirteen_calendar_months_cover_yoy_baseline(self) -> None:
        """2025-06 至 2026-06 虽只隔 365 天，但已有可按日历年对齐的去年同期。"""
        # Arrange

        rows = [{"year_month": f"2025-{month:02d}", "net_sales": 100} for month in range(6, 13)]
        rows += [{"year_month": f"2026-{month:02d}", "net_sales": 100} for month in range(1, 7)]

        # Act
        reason = _judge({"question": "按年月统计净销售额并给出同比"}, rows)

        # Assert
        assert reason is None

    # 方法作用：验证要求环比但只有一个周期时触发补查。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_single_period_with_mom_request_triggers_supplement(self) -> None:
        """环比产出方要求至少 3 个观测点，单月样本连上一期都没有。"""
        # Arrange

        rows = [{"year_month": "2026-06", "net_sales": 100}]

        # Act
        reason = _judge({"question": "按年月统计净销售额并给出环比"}, rows)

        # Assert
        assert reason and "环比" in reason

    # 方法作用：验证恰好两个周期仍判为不足。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_two_periods_still_missing_mom_baseline(self) -> None:
        """环比产出方要求至少 3 个观测点，两个周期过去既不产环比也不补查。"""
        # Arrange

        rows = [{"year_month": "2026-05", "net_sales": 90}, {"year_month": "2026-06", "net_sales": 100}]

        # Act
        reason = _judge({"question": "按年月统计净销售额并给出环比"}, rows)

        # Assert
        assert reason and "环比" in reason
        assert "2 个时间周期" in reason and "3 个周期" in reason

    # 方法作用：验证三个周期满足环比要求。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_three_periods_satisfy_mom_requirement(self) -> None:
        """达到产出方的观测点门槛后，环比不再算缺基期。"""
        # Arrange

        rows = [{"year_month": f"2026-{month:02d}", "net_sales": 90 + month} for month in range(4, 7)]

        # Act / Assert
        assert _judge({"question": "按年月统计净销售额并给出环比"}, rows) is None

    # 方法作用：验证取不到时间列时不误报。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unresolvable_time_column_does_not_trigger(self) -> None:
        """无法判断跨度就不得要求补查。"""
        # Arrange

        rows = [{"region": "华东", "net_sales": 100}]
        unit = {"question": "按地区统计净销售额并给出同比"}

        # Act / Assert
        assert _judge(unit, rows) is None

    # 方法作用：验证空样本不触发补查（另有"无数据"路径负责披露）。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_empty_rows_do_not_trigger(self) -> None:
        """没有样本就没有跨度可判，不得凭空要求补查。"""
        # Arrange

        # Act / Assert
        assert _judge({"question": "按年月统计净销售额并给出同比"}, []) is None

    # 方法作用：验证对象形态的分析单元同样可用。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_analysis_unit_object_is_supported(self) -> None:
        """运行时传的是 AnalysisUnit 实例，不是字典。"""
        # Arrange
        from src.reporting.models import AnalysisUnit

        unit = AnalysisUnit(unit_id="sales", question="按年月统计净销售额并给出同比")
        rows = [{"year_month": "2026-06", "net_sales": 100}]

        # Act
        reason = _judge(unit, rows)

        # Assert
        assert reason and "同比" in reason


class TestDecideSupplementWiring:
    """覆盖新条件接入既有四条前置筛选（与「显著发现」是或关系）。"""

    # 方法作用：验证无任何显著发现时靠缺基期也能进入补查回路。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_triggers_without_any_finding(self) -> None:
        """事故回归：无发现 + 缺基期必须补查。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        state = {
            "plan": _plan(),
            "unit_results": [_result("sales", [{"year_month": "2026-06", "net_sales": 100}])],
            "unit_findings": [],
        }

        # Act / Assert
        assert report_decide_supplement(state) == "supplement"

    # 方法作用：验证低显著度发现不会挡住缺基期触发。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_low_significance_findings_do_not_block(self) -> None:
        """只有 low 发现时，既有门槛不满足，新条件必须独立放行。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        state = {
            "plan": _plan(),
            "unit_results": [_result("sales", [{"year_month": "2026-06", "net_sales": 100}])],
            "unit_findings": [_finding("low")],
        }

        # Act / Assert
        assert report_decide_supplement(state) == "supplement"

    # 方法作用：验证有显著发现时既有行为不变。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_significant_finding_still_enters_loop(self) -> None:
        """新增条件只做或运算，不得让既有触发路径失效。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement
        from src.reporting.models import AnalysisUnit

        state = {
            "plan": _plan(units=[AnalysisUnit(unit_id="sales", question="销售趋势")]),
            "unit_results": [_result("sales", [{"region": "华东", "amount": 1}])],
            "unit_findings": [_finding("high")],
        }

        # Act / Assert
        assert report_decide_supplement(state) == "supplement"

    # 方法作用：验证跨度足够且无显著发现时仍然不进回路。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sufficient_span_without_finding_still_skips(self) -> None:
        """没有触发条件时必须继续后续节点，避免无谓的模型调用。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        rows = [{"year_month": f"2026-{month:02d}", "net_sales": 100 + month} for month in range(1, 13)]
        rows.append({"year_month": "2025-06", "net_sales": 90})
        state = {"plan": _plan(), "unit_results": [_result("sales", rows)], "unit_findings": []}

        # Act / Assert
        assert report_decide_supplement(state) == "continue"

    # 方法作用：验证新触发条件不消耗模型调用。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_decision_does_not_touch_model(self, monkeypatch) -> None:
        """这条筛选的既有性质是不消耗 LLM 调用，接入后必须保持。"""
        # Arrange
        import src.llm.client as llm_client
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        def boom(task: str) -> bool:
            """决策阶段触碰模型可用性即视为违约。"""
            raise AssertionError("补查决策不得调用模型")

        monkeypatch.setattr(llm_client, "is_task_llm_available", boom)
        state = {
            "plan": _plan(),
            "unit_results": [_result("sales", [{"year_month": "2026-06", "net_sales": 100}])],
            "unit_findings": [],
        }

        # Act / Assert
        assert report_decide_supplement(state) == "supplement"

    # 方法作用：验证缺基期不得绕过额度与轮次闸门。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_budget_and_round_gates_still_first(self) -> None:
        """既有四条前置条件仍然先判：保守档 0 额度、标准档一轮到顶都不得补查。"""
        # Arrange
        from src.graph.subgraphs.report_workbench import report_decide_supplement

        base = {
            "unit_results": [_result("sales", [{"year_month": "2026-06", "net_sales": 100}])],
            "unit_findings": [],
        }

        # Act / Assert
        assert report_decide_supplement({**base, "plan": _plan("conservative")}) == "continue"
        assert report_decide_supplement({**base, "plan": _plan(), "divergence_round": 1}) == "continue"

    # 方法作用：验证事故场景在真实子图里确实走进化补查回路并收敛。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_subgraph_enters_supplement_round_on_single_month(self, monkeypatch) -> None:
        """只拿回单月数据且一条发现都产不出时，图必须走一轮补查后正常结束。"""
        # Arrange
        import src.llm.client as llm_client
        from src.graph.subgraphs.report_workbench import build_report_workbench_subgraph
        from src.reporting.models import UnitResult

        # 补查阶段声明无可用模型：节点降级为空建议，测试不触碰任何真实模型。
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)
        plan = _plan()

        async def execute(unit, _state):
            """只返回单月样本，事故里就是这样拿不到基期。"""
            return UnitResult(
                unit_id=unit.unit_id,
                datasource="sales",
                rows=[{"year_month": "2026-06", "net_sales": 100}],
                columns=["year_month", "net_sales"],
            )

        # Act
        result = await build_report_workbench_subgraph().ainvoke({
            "plan": plan,
            "identity": {"tenant_id": 3, "user_id": 7, "role": "analyst"},
            "execute_unit": execute,
        })

        # Assert：轮数被推进说明确实进过补查节点，且回路在轮次上限处收敛
        assert result["divergence_round"] == 1
        assert result["run_status"] == "succeeded"


class TestJudgementMatchesProducer:
    """不变量：判定说「足够」当且仅当产出方确实产得出指标（本次评审修复的结构保证）。

    判定侧（needs_baseline_supplement）与产出侧（_period_findings）只共用同一处门槛，
    本类逐指标对齐两者结论：任一处的口径被单独改动（例如判定改回日历月、产出方改回
    「2 个周期算够」），下面这条参数化断言立刻变红。
    """

    # 方法作用：验证判定结论与产出方的实际产出逐指标一致。
    # Args: self - 测试实例；case - 用例名；rows - 代表性跨度的样本行。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("case", "rows"),
        [
            ("单月", _monthly_rows(["2026-06"])),
            ("恰好 2 个周期", _monthly_rows(["2026-05", "2026-06"])),
            ("恰好 3 个周期", _monthly_rows(_month_labels(2026, 4, 3))),
            ("12 个日历月", _monthly_rows(_month_labels(2025, 1, 12))),
            ("13 个日历月（365 天）", _monthly_rows(_month_labels(2025, 6, 13))),
            ("14 个日历月（396 天）", _monthly_rows(_month_labels(2025, 1, 14))),
            ("365 天日粒度", _daily_rows("2025-01-01", 366)),
            ("390 天日粒度", _daily_rows("2025-01-01", 391)),
            ("400 天日粒度", _daily_rows("2025-01-01", 401)),
        ],
    )
    def test_judgement_sufficient_iff_indicator_produced(self, case: str, rows: list[dict[str, Any]]) -> None:
        """判定里说缺哪一项，产出方就必须确实没产那一项；反之同理。"""
        # Arrange：单元结果一次性构造，列集合由 UnitResult 自己派生
        from src.reporting.models import AnalysisUnit, UnitResult
        from src.reporting.profiling import needs_baseline_supplement, profile_inputs, profile_unit

        question = "按年月统计净销售额并给出同比与环比"
        unit = AnalysisUnit(unit_id="sales", question=question)
        result = UnitResult(unit_id="sales", rows=rows)
        # 两侧输入都从同一处生产派生取，判定侧不得自带一套列/指标口径
        columns, metrics = profile_inputs(result, ["net_sales"])

        # Act：同一份样本分别喂给判定侧与产出侧
        reason = needs_baseline_supplement(unit, list(result.rows), findings=[], columns=columns, metrics=metrics)
        produced = {
            finding.title
            for finding in profile_unit(unit_id="sales", rows=list(result.rows), columns=columns, metrics=metrics)
            if finding.category == "period_over_period"
        }

        # Assert：逐指标对齐，两处门槛脱钩时这里必红
        assert ("同比" in (reason or "")) == ("同比变化" not in produced), case
        assert ("环比" in (reason or "")) == ("环比变化" not in produced), case

    # 方法作用：验证真正达标的跨度两侧一致地都判「够」且都产得出。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_truly_sufficient_span_produces_both_indicators(self) -> None:
        """正例：存在日历年对齐基期且观测点 ≥3 时，既不补查、两个指标也都拿得到。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, UnitResult
        from src.reporting.profiling import needs_baseline_supplement, profile_inputs, profile_unit

        rows = _daily_rows("2025-01-01", 401)
        unit = AnalysisUnit(unit_id="sales", question="按年月统计净销售额并给出同比与环比")
        result = UnitResult(unit_id="sales", rows=rows)
        columns, metrics = profile_inputs(result, ["net_sales"])

        # Act
        reason = needs_baseline_supplement(unit, list(result.rows), findings=[], columns=columns, metrics=metrics)
        produced = {
            finding.title
            for finding in profile_unit(unit_id="sales", rows=list(result.rows), columns=columns, metrics=metrics)
            if finding.category == "period_over_period"
        }

        # Assert
        assert reason is None
        assert {"环比变化", "同比变化"} <= produced


class TestSameSourceDerivation:
    """不变量：判定侧与产出侧的输入派生必须同源（本次评审修复的结构保证）。

    判定侧曾自带一套派生（列取行键出现顺序、指标取未过授权过滤的 unit.metrics），
    产出侧取 result.columns 与 resolved_units 里已过滤的 metrics。授权过滤掉单元
    声明的指标列、且样本里有多个数值列时，两侧会选到不同数值列（detect_value_column
    对列序敏感）→ 周期数不同 → 「判足够但产出方产不出」的单向死区重开。
    """

    # 方法作用：授权过滤掉单元声明的指标列时，判定与产出仍必须落在同一个数值列上。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_authorization_filtered_metric_keeps_sides_aligned(self) -> None:
        """事故回归：判定侧走自带派生会判「基期足够」，产出方却一条同比都产不出。"""
        # Arrange：单元声明 net_sales，授权只留下 amount；两个数值列覆盖面不同
        from src.graph.subgraphs.report_workbench import (
            report_analyze_units,
            report_decide_supplement,
        )
        from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult

        months = _month_labels(2025, 1, 14)
        # net_sales 覆盖 14 个日历月、首尾相隔 396 天（够产出同比）
        rows: list[dict[str, Any]] = [
            {"year_month": label, "net_sales": 100 + index, "amount": None}
            for index, label in enumerate(months)
        ]
        # amount 只在最后一行有值：按 amount 数只有 1 个周期，同环比一条都产不出
        rows[-1]["amount"] = 500
        unit = AnalysisUnit(unit_id="sales", question="按年月统计净销售额并给出同比", metrics=["net_sales"])
        result = UnitResult(unit_id="sales", rows=rows, columns=["year_month", "amount"])
        state = {
            "plan": ReportPlan(divergence="standard", requested_datasources=["sales"], analysis_units=[unit]),
            "unit_results": [result],
            # resolved_units 的 metrics 已过授权过滤：net_sales 被过滤掉，只剩 amount
            "resolved_units": [{
                "unit_id": "sales",
                "question": unit.question,
                "columns": ["year_month", "amount"],
                "metrics": ["amount"],
            }],
        }

        # Act：产出侧先算发现，判定侧据同一份状态决策
        findings = list((await report_analyze_units(state))["unit_findings"])
        verdict = report_decide_supplement({**state, "unit_findings": findings})

        # Assert：产出方产不出同环比，判定侧就必须补查，不能报「基期足够」
        assert all(finding.category != "period_over_period" for finding in findings)
        assert verdict == "supplement"


class TestBaselineReasonReachesModelAndPage:
    """覆盖本次修复：缺基期理由必须进提示词、不被 supports_finding 过滤、并进页面。

    真实路径（is_task_llm_available=True）下，零发现的单月样本此前走不通：
    findings 段为空、无 supports_finding 的建议被整批滤掉，回路进了、模型也调了，
    却结构性地产不出任何补查单元，同比依旧为 null。
    """

    # 方法作用：在真实模型路径下验证理由进提示词且无 supports_finding 的建议不被过滤。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_baseline_reason_reaches_prompt_and_survives_filter(self, monkeypatch) -> None:
        """零发现 + 单月样本：理由必须进 sections，建议必须留下并真正执行。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.graph.subgraphs.report_workbench import report_plan_supplement
        from src.llm.output_contracts import ReportSupplementOutput
        from src.reporting.document import build_report_document
        from src.reporting.models import UnitResult

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)
        captured: list[dict[str, Any]] = []

        # 方法作用：记录补查提示词现场，并返回一条没有 supports_finding 的建议。
        # Args: prompt_id - 提示词标识；sections - 动态分区；kwargs - 其余调用参数。
        # Returns: 补查输出契约实例。
        async def fake_invoke(prompt_id, sections, **kwargs):
            captured.append({"prompt_id": prompt_id, "sections": list(sections), **kwargs})
            return ReportSupplementOutput(proposed_units=[
                {"question": "回看去年同期净销售额", "rationale": "补齐去年同期基期", "supports_finding": ""},
            ])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke)
        executed: list[str] = []

        # 方法作用：执行补查单元并返回最小可用结果。
        # Args: unit - 待执行单元；state - 当前状态。
        # Returns: 成功的单元结果。
        async def executor(unit, _state):
            executed.append(unit.unit_id)
            return UnitResult(unit_id=unit.unit_id, status="success", rows=[{"year_month": "2025-06", "net_sales": 90}])

        plan = _plan()
        state = {
            "plan": plan,
            "unit_findings": [],
            "unit_results": [_result("sales", [{"year_month": "2026-06", "net_sales": 100}])],
            "resolved_units": [{"unit_id": "sales", "question": "按年月统计净销售额并给出同比", "metrics": ["net_sales"]}],
            "authorized_datasources": {"sales": {"allowed_columns": ["year_month", "net_sales"]}},
            "execute_unit": executor,
            "divergence_round": 0,
        }

        # Act
        update = await report_plan_supplement(state)

        # Assert：判定理由确实进了提示词 sections（零发现时模型唯一可依据的东西）
        assert len(captured) == 1
        sections = {section.name: section.content for section in captured[0]["sections"]}
        prompt_text = "".join(sections.values())
        assert "2026-06" in prompt_text and "同比" in prompt_text
        # 触发来源被登记进 state，供后续轮次与页面复用
        assert update["supplement_context"]["trigger"] == "missing_baseline"
        assert "2026-06" in update["supplement_context"]["reason"]
        # 无 supports_finding 的建议没有被过滤掉，且真的被执行了
        added = list(update["runtime_units"])
        assert [unit.question for unit in added] == ["回看去年同期净销售额"]
        assert added[0].time_range.kind == "relative"
        assert added[0].time_range.value == "456d"
        assert executed == [added[0].unit_id]
        # 理由进页面 quality 块（复用 UnitResult.warnings → document 的通路）
        annotated = next(item for item in update["unit_results"] if item.unit_id == "sales")
        artifact = build_report_document(plan, [annotated])
        section = next(item for item in artifact.document.sections if item.id == "sales")
        quality = next(item for item in section.blocks if item.type == "quality")
        assert any("2026-06" in item and "同比" in item for item in quality.content["limitations"])


class TestBaselineTimeRangeExpansion:
    """覆盖缺同比补查时间窗扩展的相对、绝对与兼容边界。"""

    # 方法作用：验证相对窗口按原单位多覆盖一个完整年度。
    # Args: self - 测试实例；value - 原时间范围；expected - 扩展后范围。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("90d", "456d"), ("8w", "61w"), ("6m", "18m"), ("2q", "6q"), ("1y", "2y")],
    )
    def test_relative_range_expands_in_original_unit(self, value: str, expected: str) -> None:
        """相对范围保持单位，闰年边界不会少覆盖一天。"""
        # Arrange
        from src.reporting.models import TimeRange
        from src.reporting.planner import expand_time_range_for_yoy

        # Act
        expanded = expand_time_range_for_yoy(TimeRange(kind="relative", value=value))

        # Assert
        assert expanded == TimeRange(kind="relative", value=expected)

    # 方法作用：验证闰日绝对窗口向前平移时退到二月末。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_absolute_range_moves_start_back_one_calendar_year(self) -> None:
        """2024-02-29 的去年同期应为 2023-02-28，结束日期保持不变。"""
        # Arrange
        from src.reporting.models import TimeRange
        from src.reporting.planner import expand_time_range_for_yoy

        # Act
        expanded = expand_time_range_for_yoy(
            TimeRange(kind="absolute", value="2024-02-29/2024-03-31")
        )

        # Assert
        assert expanded.value == "2023-02-28/2024-03-31"

    # 方法作用：验证全量窗口无需扩大且不会被就地修改。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_all_range_is_preserved(self) -> None:
        """全量查询已经覆盖基期，扩展应保持等值但返回独立对象。"""
        # Arrange
        from src.reporting.models import TimeRange
        from src.reporting.planner import expand_time_range_for_yoy

        original = TimeRange(kind="all", value="all")

        # Act
        expanded = expand_time_range_for_yoy(original)

        # Assert
        assert expanded == original
        assert expanded is not original

    # 方法作用：验证旧格式相对值按保守默认天数回退。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_legacy_relative_range_uses_bounded_fallback(self) -> None:
        """无法识别单位的旧值不得抛错，也不能仍保留原窄窗口。"""
        # Arrange
        from src.reporting.models import TimeRange
        from src.reporting.planner import expand_time_range_for_yoy

        # Act
        expanded = expand_time_range_for_yoy(TimeRange(kind="relative", value="legacy"))

        # Assert
        assert expanded == TimeRange(kind="relative", value="456d")
