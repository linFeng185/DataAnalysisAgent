"""报表单元时间范围注入执行链路的回归测试。

覆盖缺陷：计划在预览阶段已为每个单元绑定 time_range（默认 90d），但
ReportRunner._default_execute_unit 调主图时只传了 unit.question，时间范围从未
进入执行链路。主图 classify_intent 按问题文本重判 needs_time_range，
build_task_plan 的规则对「trend/aggregation/attribution 且问题无时间词」判 True，
于是单元被拦成「请指定查询的时间范围」，白白丢掉一个参数早已确定的单元。

修复方式是把计划时间范围渲染成中文说明拼进执行用查询文本。
"""

from __future__ import annotations

from src.graph.contracts import build_task_plan
from src.reporting.models import AnalysisUnit, TimeRange
from src.reporting.runner import _compose_unit_query, _describe_time_range

# 不含任何时间词的单元问题，用于复现 needs_time_range 被误判的场景。
_TIMELESS_QUESTION = "对比配送费用、采购加权单位成本、库存水平与订单量的走势"


class TestDescribeTimeRange:
    """覆盖时间范围契约到中文说明的渲染。"""

    # 方法作用：相对天数按「最近 N 天」渲染。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_relative_days_rendered_as_chinese(self) -> None:
        assert _describe_time_range(TimeRange(kind="relative", value="90d")) == "最近 90 天"

    # 方法作用：相对年数按「最近 N 年」渲染。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_relative_years_rendered_as_chinese(self) -> None:
        assert _describe_time_range(TimeRange(kind="relative", value="2y")) == "最近 2 年"

    # 方法作用：all 档渲染为「全部数据」，且命中「全部」这一时间标记。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_all_kind_rendered_as_full_history(self) -> None:
        assert _describe_time_range(TimeRange(kind="all", value="90d")) == "全部数据"

    # 方法作用：绝对范围保留原始日期串并补中文单位词，保证规则可识别。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_absolute_kind_keeps_raw_range(self) -> None:
        text = _describe_time_range(TimeRange(kind="absolute", value="2026-01-01:2026-12-31"))
        assert "2026-01-01:2026-12-31" in text
        assert "日" in text

    # 方法作用：无法解析的相对值原样返回，不能凭空丢弃用户设定。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_unparsable_relative_value_falls_back_to_raw(self) -> None:
        assert _describe_time_range(TimeRange(kind="relative", value="custom")) == "custom"

    # 方法作用：缺失时间范围时返回空串，由调用方决定是否拼装。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_missing_time_range_returns_empty(self) -> None:
        assert _describe_time_range(None) == ""


class TestComposeUnitQuery:
    """覆盖执行用查询文本的拼装。"""

    # 方法作用：问题末尾追加时间范围说明，不改写原问题本身。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_appends_time_range_to_question(self) -> None:
        unit = AnalysisUnit(
            unit_id="unit_a",
            question="库存水平走势",
            time_range=TimeRange(kind="relative", value="90d"),
        )
        assert _compose_unit_query(unit) == "库存水平走势（时间范围：最近 90 天）"

    # 方法作用：回归缺陷——注入时间范围后，主图不再要求用户补时间范围。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_injection_defeats_needs_time_range_rule(self) -> None:
        unit = AnalysisUnit(
            unit_id="unit_a",
            question=_TIMELESS_QUESTION,
            time_range=TimeRange(kind="relative", value="90d"),
        )

        # 前提：原始问题确实会被判成需要补时间范围。
        assert build_task_plan("trend", query=unit.question).needs_time_range is True
        # 修复后：拼上计划时间范围的执行文本不再触发补参。
        injected = _compose_unit_query(unit)
        assert build_task_plan("trend", query=injected).needs_time_range is False

    # 方法作用：全部数据档同样不再被判为缺少时间范围。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_all_kind_injection_defeats_needs_time_range_rule(self) -> None:
        unit = AnalysisUnit(
            unit_id="unit_a",
            question=_TIMELESS_QUESTION,
            time_range=TimeRange(kind="all", value="all"),
        )
        assert build_task_plan("aggregation", query=_compose_unit_query(unit)).needs_time_range is False
