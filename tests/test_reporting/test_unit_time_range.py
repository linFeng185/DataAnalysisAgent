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

        # Assert：先保证列表非空，否则下面的循环会空转假绿。
        assert plan.analysis_units
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

    # 方法作用：验证中文绝对区间被拒后单元不消失，落回规则抽取的安全值。
    async def test_chinese_absolute_value_keeps_unit_with_safe_fallback(self, monkeypatch) -> None:
        """评审复现：模型写「2016年1月1日至2036年12月31日」，此前整单元连同业务问题一起消失。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportPlanUnitsOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        # 方法作用：返回一个带中文绝对区间单元加一个正常单元的模型输出契约实例。
        # Args: prompt_id - 注册的 Prompt 标识；sections - 提示段落；kwargs - 透传参数。
        # Returns: 含两个分析单元的模型输出契约实例。
        async def fake_invoke_structured(prompt_id, sections, **kwargs):
            return ReportPlanUnitsOutput(
                analysis_units=[
                    {
                        "unit_id": "sales",
                        "question": "按年月统计订单净销售额",
                        "time_range": {"kind": "absolute", "value": "2016年1月1日至2036年12月31日"},
                    },
                    {"unit_id": "target", "question": "按门店对比销售目标与实际净销售额"},
                ]
            )

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke_structured)

        # Act
        plan = await ReportPlanner().parse(_ACCIDENT_PROMPT, datasources=["sales"])

        # Assert：非法时间范围不得带走任何业务单元，且该单元落回规则抽取的安全值。
        assert {unit.unit_id for unit in plan.analysis_units} == {"sales", "target"}
        unit = next(item for item in plan.analysis_units if item.unit_id == "sales")
        assert unit.time_range.kind == "absolute"
        assert unit.time_range.value == "2016-01-01/2036-12-31"

    # 方法作用：验证 all 档中文值被拒后落回默认相对值，单元不消失。
    async def test_all_kind_with_chinese_value_falls_back_to_default(self, monkeypatch) -> None:
        """评审复现：模型写 {"kind": "all", "value": "全部"}，非 ASCII 值此前同样带走整单元。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as llm_invocation
        from src.llm.output_contracts import ReportPlanUnitsOutput
        from src.reporting.planner import ReportPlanner

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        # 方法作用：返回带中文 all 时间范围单元的模型输出契约实例。
        # Args: prompt_id - 注册的 Prompt 标识；sections - 提示段落；kwargs - 透传参数。
        # Returns: 含单个带中文 all 值单元的模型输出契约实例。
        async def fake_invoke_structured(prompt_id, sections, **kwargs):
            return ReportPlanUnitsOutput(
                analysis_units=[
                    {
                        "unit_id": "sales",
                        "question": "按年月统计订单净销售额",
                        "time_range": {"kind": "all", "value": "全部"},
                    }
                ]
            )

        monkeypatch.setattr(llm_invocation, "invoke_structured", fake_invoke_structured)

        # Act：需求里不含任何时间范围，兜底只能落到默认相对值。
        plan = await ReportPlanner().parse("1. 销售：按年月统计订单净销售额；2. 库存：按商品统计库存量。", datasources=["sales"])

        # Assert
        assert len(plan.analysis_units) == 1
        assert plan.analysis_units[0].question == "按年月统计订单净销售额"
        assert plan.analysis_units[0].time_range.kind == "relative"
        assert plan.analysis_units[0].time_range.value == "90d"

    # 方法作用：验证抽取器自身的 canonical 区间可被重解析，且单个日期不误判。
    def test_canonical_range_is_reparsable(self) -> None:
        """`/` 是抽取器 canonical 输出的连接符，必须能重解析；单日期不得误判为区间。"""
        # Arrange
        from src.reporting.models import TimeRange
        from src.reporting.planner import extract_time_range

        # Act & Assert：canonical 与不规范斜杠写法都规范化为同一结果。
        assert extract_time_range("2016-01-01/2036-12-31") == TimeRange(kind="absolute", value="2016-01-01/2036-12-31")
        assert extract_time_range("2016-1-1/2036-12-31") == TimeRange(kind="absolute", value="2016-01-01/2036-12-31")
        # 单个斜杠日期没有连接词，不能被误判成区间。
        assert extract_time_range("2016/01/01") is None

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


class TestTimeRangePositionRule:
    """覆盖 2026-09-16 事故：用户在开头写「时间范围是去年」，计划却取了后文的日期区间。"""

    # 方法作用：验证最早出现的时间写法胜出，而不是绝对区间无条件优先。
    def test_earliest_expression_wins(self) -> None:
        """需求第一句才是报表级口径，后文顺带提到的日期区间不能覆盖它。"""
        # Arrange
        from src.reporting.planner import extract_time_range

        prompt = "立即生成一份《集团经营全景分析报表》，时间范围是去年，页面保留 30 天，并允许团队成员查看。"

        # Act
        result = extract_time_range(prompt)

        # Assert
        assert result is not None
        year = int(result.value[:4])
        # 去年＝当前年份减一，起止为整年。
        assert result.value == f"{year}-01-01/{year}-12-31"

    # 方法作用：验证粘回需求里的上一版计划渲染块不参与时间范围解析。
    def test_rendered_plan_block_is_not_parsed_as_requirement(self) -> None:
        """实机需求里，「你要求的分析」之后的单元文本带着 2016–2036 的旧区间。"""
        # Arrange
        from src.reporting.planner import extract_time_range

        prompt = (
            "立即生成一份《集团经营全景分析报表》，时间范围是去年，页面保留 30 天。\n"
            "你要求的分析\n"
            "按年月统计净销售额，时间范围 2016-01-01 至 2036-12-31。\n"
            "数据源：待绑定"
        )

        # Act
        result = extract_time_range(prompt)

        # Assert
        assert result is not None
        assert "2016-01-01" not in result.value
        assert "2036-12-31" not in result.value

    # 方法作用：验证需求正文里本来就带绝对区间时仍以它为准。
    def test_leading_absolute_range_still_wins(self) -> None:
        """按位置排序不能把用户明确写在前面的绝对区间让给后文的口语说法。"""
        # Arrange
        from src.reporting.models import TimeRange
        from src.reporting.planner import extract_time_range

        # Act
        result = extract_time_range("时间范围 2024-01-01 至 2024-12-31，统计去年同期的对比")

        # Assert
        assert result == TimeRange(kind="absolute", value="2024-01-01/2024-12-31")
