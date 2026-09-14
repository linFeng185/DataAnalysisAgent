"""单元时间范围抽取契约（复现 2026-09-14 事故）。"""

from __future__ import annotations

_ACCIDENT_PROMPT = (
    "立即生成一份《集团经营全景分析报表》，时间范围 2016-01-01 至 2036-12-31，页面保留 30 天。\n"
    "1. 销售基盘：按年月统计订单净销售额、订单数；\n"
    "2. 目标达成：按门店和年月对比销售目标与实际净销售额。"
)


class TestUnitTimeRange:
    """覆盖时间范围从需求到单元、再到查询的传递。"""

    # 方法作用：验证需求里的绝对时间范围落到每个单元。
    async def test_absolute_range_reaches_every_unit(self, monkeypatch) -> None:
        """事故：prompt 写了十年区间，22 个单元却全是默认 90d。"""
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        plan = await ReportPlanner().parse(_ACCIDENT_PROMPT, datasources=["sales"])

        # Assert
        for unit in plan.analysis_units:
            assert unit.time_range.kind == "absolute"
            assert unit.time_range.value == "2016-01-01/2036-12-31"

    # 方法作用：验证需求未写时间时保持相对默认值。
    async def test_missing_range_keeps_relative_default(self, monkeypatch) -> None:
        # Arrange
        import src.llm.client as llm_client
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # Act
        plan = await ReportPlanner().parse("1. 销售：按年月统计净销售额；2. 库存：按商品统计库存量。", datasources=["sales"])

        # Assert
        assert plan.analysis_units[0].time_range.kind == "relative"

    # 方法作用：验证模型给出的 time_range 优先生效，不被规则层兜底覆盖。
    async def test_model_time_range_wins_over_rule_fallback(self, monkeypatch) -> None:
        """模型已按提示词抽到的时间范围必须保留，只兜底默认值单元。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportPlanUnitsOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        # 方法作用：返回带 time_range 的模型结构化输出。
        # Args: prompt_id - 注册的 Prompt 标识；sections - 提示段落；kwargs - 透传参数。
        # Returns: 含单个带时间范围单元的输出契约实例。
        async def fake_invoke_structured(prompt_id, sections, **kwargs):
            return ReportPlanUnitsOutput(
                analysis_units=[
                    {
                        "unit_id": "sales",
                        "question": "按年月统计订单净销售额",
                        "time_range": {"kind": "relative", "value": "30d"},
                    }
                ]
            )

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke_structured)

        # Act
        plan = await ReportPlanner().parse(_ACCIDENT_PROMPT, datasources=["sales"])

        # Assert
        assert plan.analysis_units[0].time_range.kind == "relative"
        assert plan.analysis_units[0].time_range.value == "30d"

    # 方法作用：验证模型未给出 time_range 时由规则层抽取兜底。
    async def test_model_missing_range_falls_back_to_rule_extraction(self, monkeypatch) -> None:
        """模型没抽到时间范围时，需求里的绝对区间必须由服务端补上。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportPlanUnitsOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        # 方法作用：返回不带 time_range 的模型结构化输出。
        # Args: prompt_id - 注册的 Prompt 标识；sections - 提示段落；kwargs - 透传参数。
        # Returns: 含单个无时间范围单元的输出契约实例。
        async def fake_invoke_structured(prompt_id, sections, **kwargs):
            return ReportPlanUnitsOutput(analysis_units=[{"unit_id": "sales", "question": "按年月统计订单净销售额"}])

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke_structured)

        # Act
        plan = await ReportPlanner().parse(_ACCIDENT_PROMPT, datasources=["sales"])

        # Assert
        assert plan.analysis_units[0].time_range.kind == "absolute"
        assert plan.analysis_units[0].time_range.value == "2016-01-01/2036-12-31"

    # 方法作用：验证时间范围被拼进单元查询。
    def test_query_includes_absolute_range(self) -> None:
        # Arrange
        from src.reporting.models import AnalysisUnit, TimeRange
        from src.reporting.runner import _compose_unit_query

        unit = AnalysisUnit(unit_id="u1", question="按年月统计净销售额", time_range=TimeRange(kind="absolute", value="2016-01-01/2036-12-31"))

        # Act
        query = _compose_unit_query(unit)

        # Assert
        assert "2016-01-01" in query and "2036-12-31" in query
