"""图表方案契约：类型由模型按数据形态决策，多图/多系列/可切换三种形态可用。

覆盖计划 Task 5（复现 2026-09-14 事故：20 个图表块 series.type 全是 bar）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.llm.output_contracts import ChartSpec, ReportChartPlan
from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult


# 方法作用：构造一个带时间列与两个数值列的单元结果。
# Args: rows - 可选行数据，缺省给 6 个月。
# Returns: 单元结果实例。
def _result(rows: list[dict] | None = None) -> UnitResult:
    return UnitResult(
        unit_id="u1",
        columns=["year_month", "region_code", "channel_code", "net_sales", "order_count"],
        rows=rows or [
            {
                "year_month": f"2016-{month:02d}",
                "region_code": region,
                "channel_code": channel,
                "net_sales": month,
                "order_count": month * 2,
            }
            for month in range(1, 7)
            for region, channel in (("CN-BJ", "官网"), ("CN-GD", "移动应用"))
        ],
    )


# 方法作用：构造只含该单元的计划。
# Args: 无。
# Returns: 报表计划实例。
def _plan() -> ReportPlan:
    return ReportPlan(analysis_units=[AnalysisUnit(unit_id="u1", question="按年月统计净销售额与订单数")])


# 方法作用：取出产物里所有图表块。
# Args: artifact - 报表产物。
# Returns: 图表块列表。
def _chart_blocks(artifact) -> list:
    return [block for section in artifact.document.sections for block in section.blocks if block.type == "chart"]


# 方法作用：构造一个可替换 _invoke_chart_plan 的假实现。
# Args: specs - 期望模型返回的图表方案序列。
# Returns: 异步假函数，返回由这些方案组成的 ReportChartPlan。
def _fake_invoke(*specs: ChartSpec):
    async def _invoke(**kwargs) -> ReportChartPlan:
        del kwargs
        return ReportChartPlan(charts=list(specs))

    return _invoke


class TestChartTypeComesFromDecision:
    """覆盖图表类型来源与三种呈现形态。"""

    # 方法作用：验证图表类型来自决策结果而不是硬编码 bar。
    @pytest.mark.asyncio
    async def test_chart_type_comes_from_decision(self, monkeypatch) -> None:
        """事故：20 个图表块 series.type 全是 bar，与数据形态无关。"""
        # Arrange
        import src.reporting.charts as charts

        monkeypatch.setattr(charts, "_invoke_chart_plan", _fake_invoke(ChartSpec(
            chart_type="line", title="净销售额趋势", x_field="year_month", series_fields=["net_sales"],
        )))

        # Act
        plan = await charts.plan_charts(question="按年月统计净销售额", columns=["year_month", "net_sales"], rows=_result().rows)

        # Assert
        assert plan.charts[0].chart_type == "line"

    # 方法作用：验证一个单元可以产出多张图。
    @pytest.mark.asyncio
    async def test_unit_can_emit_multiple_charts(self, monkeypatch) -> None:
        """多指标单元应能按指标拆成多张图，而不是只画第一个数值列。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.document import build_report_document

        monkeypatch.setattr(charts, "_invoke_chart_plan", _fake_invoke(
            ChartSpec(chart_type="line", title="净销售额", x_field="year_month", series_fields=["net_sales"]),
            ChartSpec(chart_type="bar", title="订单数", x_field="year_month", series_fields=["order_count"]),
        ))

        # Act
        artifact = build_report_document(
            _plan(),
            [_result()],
            chart_plans={"u1": await charts.plan_charts(question="按年月统计", columns=[], rows=_result().rows)},
        )

        # Assert
        assert len(_chart_blocks(artifact)) == 2
        assert [item["name"] for item in _chart_blocks(artifact)[1].echarts_option["series"]] == ["订单数"]

    # 方法作用：验证一张图可以承载多条系列。
    @pytest.mark.asyncio
    async def test_chart_can_hold_multiple_series(self, monkeypatch) -> None:
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.document import build_report_document

        monkeypatch.setattr(charts, "_invoke_chart_plan", _fake_invoke(ChartSpec(
            chart_type="line", title="双指标", x_field="year_month", series_fields=["net_sales", "order_count"],
        )))

        # Act
        artifact = build_report_document(
            _plan(),
            [_result()],
            chart_plans={"u1": await charts.plan_charts(question="按年月统计", columns=[], rows=_result().rows)},
        )

        # Assert
        series = _chart_blocks(artifact)[0].echarts_option["series"]
        # 多指标各成一条系列；无中文注释时按词表翻译图例名。
        assert [item["name"] for item in series] == ["净销售额", "订单数"]
        assert all(item["type"] == "line" for item in series)

    # 方法作用：验证每个候选分组都算好系列并下发，切换不必重新请求后端。
    @pytest.mark.asyncio
    async def test_group_options_are_computed_for_every_candidate(self, monkeypatch) -> None:
        """切换项必须有现成数据：前端拿不到系列就会出现"点了标签数据清空"。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.document import build_report_document

        monkeypatch.setattr(charts, "_invoke_chart_plan", _fake_invoke(ChartSpec(
            chart_type="line",
            title="可切换",
            x_field="year_month",
            series_fields=["net_sales"],
            switchable_fields=["region_code", "channel_code"],
        )))

        # Act
        artifact = build_report_document(
            _plan(),
            [_result()],
            chart_plans={"u1": await charts.plan_charts(question="按年月统计", columns=[], rows=_result().rows)},
        )

        # Assert：两个候选分组各有一份非空系列（时间轴还会附加同比/环比视图）
        content = _chart_blocks(artifact)[0].content
        options = content["group_options"]
        assert {"region_code", "channel_code"} <= set(options)
        assert options["region_code"] and options["region_code"][0]["data"]
        assert options["channel_code"] and options["channel_code"][0]["data"]
        assert all(
            item["type"] == "line"
            for series in options.values()
            for item in series
        )

    # 方法作用：验证柱状图切换分组后仍是柱状图，不能被底层透视函数硬改成折线图。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_bar_group_options_keep_bar_type(self, monkeypatch) -> None:
        """分组切换下发的 series 必须保留图表决策中的 bar 类型。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.document import build_report_document

        monkeypatch.setattr(charts, "_invoke_chart_plan", _fake_invoke(ChartSpec(
            chart_type="bar",
            title="分地区销售额",
            x_field="year_month",
            series_fields=["net_sales"],
            switchable_fields=["region_code", "channel_code"],
        )))

        # Act
        artifact = build_report_document(
            _plan(),
            [_result()],
            chart_plans={"u1": await charts.plan_charts(question="按地区统计", columns=[], rows=_result().rows)},
        )

        # Assert
        options = _chart_blocks(artifact)[0].content["group_options"]
        assert options
        assert all(item["type"] == "bar" for series in options.values() for item in series)

    # 方法作用：验证饼图、散点图和热力图不会下发折线形状的分组替换数据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("chart_type", ["pie", "scatter", "heatmap"])
    def test_special_chart_types_do_not_emit_incompatible_group_options(self, chart_type: str) -> None:
        """特殊图型的数据形状不同，前端不能用 line series 整体覆盖正确 option。"""
        # Arrange
        from src.reporting.document import build_report_document

        plan = ReportChartPlan(charts=[ChartSpec(
            chart_type=chart_type,
            x_field="year_month" if chart_type != "scatter" else "net_sales",
            group_field="region_code",
            series_fields=["order_count" if chart_type == "scatter" else "net_sales"],
            switchable_fields=["channel_code"],
        )])

        # Act
        artifact = build_report_document(_plan(), [_result()], chart_plans={"u1": plan})

        # Assert
        chart = _chart_blocks(artifact)[0]
        assert chart.echarts_option["series"][0]["type"] == chart_type
        assert "group_options" not in chart.content

    # 方法作用：验证页面体积预算剥离分组视图时向用户披露，默认图仍保留。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    def test_payload_budget_trimming_is_disclosed(self, monkeypatch) -> None:
        """分组切换被体积预算删除时，页面不能看起来像从未支持过该视图。"""
        # Arrange
        import src.reporting.document as document_module

        monkeypatch.setattr(document_module, "_PAGE_PAYLOAD_BUDGET", 1)
        chart_plan = ReportChartPlan(charts=[ChartSpec(
            chart_type="line",
            x_field="year_month",
            series_fields=["net_sales"],
            switchable_fields=["region_code", "channel_code"],
        )])

        # Act
        artifact = document_module.build_report_document(_plan(), [_result()], chart_plans={"u1": chart_plan})

        # Assert
        chart = _chart_blocks(artifact)[0]
        assert "group_options" not in chart.content
        assert any("分组切换视图" in item and "1" in item for item in artifact.document.limitations)

    # 方法作用：验证决策要求出表时建 table 块而不是 chart 块。
    @pytest.mark.asyncio
    async def test_table_decision_emits_table_block(self, monkeypatch) -> None:
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.document import build_report_document

        monkeypatch.setattr(charts, "_invoke_chart_plan", _fake_invoke(ChartSpec(chart_type="table", title="数据一览")))

        # Act
        artifact = build_report_document(
            _plan(),
            [_result()],
            chart_plans={"u1": await charts.plan_charts(question="按年月统计", columns=[], rows=_result().rows)},
        )

        # Assert
        kinds = [block.type for section in artifact.document.sections for block in section.blocks]
        assert "table" in kinds
        assert "chart" not in kinds

    # 方法作用：验证同一单元有多张 table 图表时，各 table 页面块 ID 仍然唯一。
    # Args: 无。
    # Returns: 无；断言通过即表示不再触发「同一区段页面块 ID 不能重复」校验。
    def test_multiple_table_charts_keep_unique_block_ids(self) -> None:
        """事故：同单元两张 table 恒定取 table_{section_id}，构建文档即抛重复 ID 校验错误。"""
        # Arrange：同一单元放两张 table，走完整 build_report_document 构建链路。
        from src.reporting.document import build_report_document

        chart_plan = ReportChartPlan(charts=[
            ChartSpec(chart_type="table", title="明细一"),
            ChartSpec(chart_type="table", title="明细二"),
        ])

        # Act：构建不应因重复 ID 抛异常。
        artifact = build_report_document(_plan(), [_result()], chart_plans={"u1": chart_plan})

        # Assert：两个 table 块都在，且 ID 互不相同。
        table_ids = [
            block.id
            for section in artifact.document.sections
            for block in section.blocks
            if block.type == "table"
        ]
        assert len(table_ids) == 2
        assert len(set(table_ids)) == len(table_ids)
        assert table_ids == ["table_u1", "table_u1_1"]


class TestChartPivot:
    """分组与聚合由代码做：重复横轴值、英文标签、取值列错都必须被挡住。"""

    # 方法作用：验证横轴去重，同一月份不会重复出现多个数据点。
    def test_x_axis_is_deduplicated(self) -> None:
        """实机事故：100 行明细直接铺开，x 轴出现 100 个重复的 2016-01。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": f"2016-{month:02d}", "region_code": region, "net_sales": month}
            for month in range(1, 4)
            for region in ("CN-BJ", "CN-GD", "CN-SH")
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["xAxis"]["data"] == ["2016-01", "2016-02", "2016-03"]
        assert len(option["series"]) == 3

    # 方法作用：验证每个分组一条系列，点数是横轴长度而非明细行数。
    def test_series_split_by_group_field(self) -> None:
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 10},
            {"year_month": "2016-01", "region_code": "CN-GD", "net_sales": 20},
            {"year_month": "2016-02", "region_code": "CN-BJ", "net_sales": 30},
            {"year_month": "2016-02", "region_code": "CN-GD", "net_sales": 40},
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        # 没有中文注释时列名走确定性词表翻译，图例里不再出现 region_code。
        by_name = {item["name"]: item["data"] for item in option["series"]}
        assert by_name["地区编码：CN-BJ"] == [10, 30]
        assert by_name["地区编码：CN-GD"] == [20, 40]

    # 方法作用：验证同一 (x, 分组) 的多行会按列语义聚合，而不是各占一个点。
    def test_duplicate_cells_are_aggregated(self) -> None:
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 10},
            {"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 5},
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert：金额类求和
        assert option["series"][0]["data"] == [15.0]

    # 方法作用：验证比率类列求平均而不是求和。
    def test_rate_columns_are_averaged(self) -> None:
        """把达成率、折扣深度这类比率列相加会得出无意义的和。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "achieve_rate": 80.0},
            {"year_month": "2016-01", "region_code": "CN-BJ", "achieve_rate": 100.0},
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["achieve_rate"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["series"][0]["data"] == [90.0]

    # 方法作用：验证 Decimal、千分位字符串与少量脏值不会让整列静默降级为维度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_numeric_column_tolerates_decimal_grouped_text_and_dirty_rows(self) -> None:
        """数据库 Decimal 与合计行中的 N/A 不得让正常指标列整列消失。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"month": "2026-01", "amount": Decimal("10.5")},
            {"month": "2026-02", "amount": "1,234.00"},
            {"month": "合计", "amount": "N/A"},
        ]
        spec = ChartSpec(chart_type="bar", x_field="month", series_fields=["amount"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["series"][0]["type"] == "bar"
        assert option["series"][0]["data"] == [10.5, 1234.0, None]

    # 方法作用：验证散点图允许数值列作为横轴，并选择另一数值列作为纵轴。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_scatter_accepts_numeric_x_axis(self) -> None:
        """散点图的 x_field 本来就应是数值列，不能按维度列规则把它清空。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"price": 10, "quantity": 2},
            {"price": 20, "quantity": 5},
        ]
        spec = ChartSpec(chart_type="scatter", x_field="price", series_fields=["quantity"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["series"][0]["data"] == [[10.0, 2.0], [20.0, 5.0]]

    # 方法作用：验证全空首选分组会退到有值候选维度，而不是生成空系列。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_empty_group_column_falls_back_to_populated_candidate(self) -> None:
        """全空列不得成为默认分组，否则每行都会被跳过并得到空图。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"month": "2026-01", "empty_group": None, "region": "华东", "amount": 10},
            {"month": "2026-02", "empty_group": None, "region": "华南", "amount": 20},
        ]
        spec = ChartSpec(
            chart_type="line",
            x_field="month",
            group_field="empty_group",
            series_fields=["amount"],
            switchable_fields=["empty_group", "region"],
        )

        # Act
        group_field, default_series, _options = build_group_options(spec=spec, rows=rows)

        # Assert
        assert group_field == "region"
        assert default_series

    # 方法作用：验证系列名与图例走中文标签，不透出英文列名。
    def test_series_names_are_localized(self) -> None:
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 10}]
        spec = ChartSpec(chart_type="bar", x_field="year_month", group_field="region_code", series_fields=["net_sales"])
        labels = {"region_code": "地区", "net_sales": "净销售额"}

        # Act
        option = build_option_for_spec(spec, rows, labels)

        # Assert：术语用中文，分组取值保持原值（地区编码本身就是可读的业务值）
        assert option["series"][0]["name"] == "地区：CN-BJ"

    # 方法作用：验证热力图的取值列来自指标列，而不是最后一个数值列。
    def test_heatmap_uses_metric_column(self) -> None:
        """实机事故：值取成 numeric[-1] 的排名列，把"排名"画成了折扣深度。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"region_code": "CN-BJ", "channel_code": "官网", "discount_depth_pct": 8.3, "channel_rank_asc": 28.0},
        ]
        spec = ChartSpec(
            chart_type="heatmap",
            x_field="region_code",
            group_field="channel_code",
            series_fields=["discount_depth_pct"],
        )

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert：热力图的值是折扣深度 8.3，不是排名 28
        assert option["series"][0]["data"] == [[0, 0, 8.3]]

    # 方法作用：验证横轴标签过多时不再逐格渲染数字，避免糊成一片。
    def test_heatmap_hides_labels_when_dense(self) -> None:
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"region_code": f"CN-{index:02d}", "channel_code": f"CH-{index:02d}", "discount_depth_pct": 1.0}
            for index in range(30)
        ]
        spec = ChartSpec(chart_type="heatmap", x_field="region_code", group_field="channel_code", series_fields=["discount_depth_pct"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["series"][0]["label"]["show"] is False

    # 方法作用：验证时间跨度大时自动上卷到年，且 x 轴与点数对齐。
    def test_long_time_span_rolls_up_to_year(self) -> None:
        """十年月度数据挤在一根轴上没人看得懂；上卷后点轴必须严格对齐。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": f"{year}-{month:02d}", "region_code": "CN-BJ", "net_sales": 1}
            for year in range(2016, 2026)
            for month in range(1, 13)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["xAxis"]["data"] == [str(year) for year in range(2016, 2026)]
        assert len(option["series"][0]["data"]) == len(option["xAxis"]["data"])
        assert option["series"][0]["data"][0] == 12.0

    # 方法作用：验证跨度小时保留原始粒度，不做无谓上卷。
    def test_short_time_span_keeps_month_grain(self) -> None:
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"year_month": f"2016-{month:02d}", "region_code": "CN-BJ", "net_sales": 1} for month in range(1, 4)]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["xAxis"]["data"] == ["2016-01", "2016-02", "2016-03"]

    # 方法作用：验证按日粒度的小跨度数据保持按日，不会误上卷。
    def test_daily_grain_is_preserved_when_short(self) -> None:
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"stat_date": f"2016-01-{day:02d}", "region_code": "CN-BJ", "net_sales": 1} for day in range(1, 11)]
        spec = ChartSpec(chart_type="line", x_field="stat_date", group_field="region_code", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["xAxis"]["data"][0] == "2016-01-01"
        assert len(option["xAxis"]["data"]) == 10

    # 方法作用：验证月度粒度下同比按 12 期回看，由代码计算。
    def test_yoy_is_computed_in_code(self) -> None:
        """同比让模型算很容易幻觉：基期取错、方向反了都不会被发现。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"year_month": f"{year}-{month:02d}", "region_code": "CN-BJ", "net_sales": 100 if year == 2016 else 200}
            for year in (2016, 2017)
            for month in range(1, 13)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows)

        # Assert：前 12 期没有基期，第 13 期（2017-01 vs 2016-01）同比 +100%
        yoy = options["__yoy__"][0]["data"]
        assert yoy[:12] == [None] * 12
        assert yoy[12] == 100.0

    # 方法作用：验证月度粒度同时提供同比与环比。
    def test_mom_is_computed_in_code(self) -> None:
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 10},
            {"year_month": "2016-02", "region_code": "CN-BJ", "net_sales": 20},
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows)

        # Assert
        mom = options["__mom__"][0]["data"]
        assert mom[0] is None
        assert mom[1] == 100.0

    # 方法作用：验证基期为零时留空而不是除零或编造。
    def test_zero_base_leaves_blank(self) -> None:
        # Arrange
        from src.reporting.charts import build_group_options

        # 两年的同月数据：2016 全零（基期为零），2017 有值
        rows = [
            {"year_month": f"{year}-{month:02d}", "region_code": "CN-BJ", "net_sales": 0 if year == 2016 else 50}
            for year in (2016, 2017)
            for month in range(1, 13)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows)

        # Assert
        assert all(value is None for value in options["__yoy__"][0]["data"])

    # 方法作用：验证日粒度不提供同比（回看 365 点不是业务意义上的同比）。
    def test_daily_grain_has_no_yoy(self) -> None:
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"stat_date": f"2016-01-{day:02d}", "region_code": "CN-BJ", "net_sales": day}
            for day in range(1, 6)
        ]
        spec = ChartSpec(chart_type="line", x_field="stat_date", group_field="region_code", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows)

        # Assert
        assert "__yoy__" not in options
        assert "__mom__" in options

    # 方法作用：验证基数在可读范围内时按量级截取系列。
    def test_many_groups_are_capped(self) -> None:
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2016-01", "region_code": f"R{index:02d}", "net_sales": index}
            for index in range(charts._MAX_GROUP_CARDINALITY)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="region_code", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert len(option["series"]) == charts._MAX_SERIES

    # 方法作用：验证分组基数过大时退化为不分组，而不是画出上百条几乎空的线。
    def test_huge_cardinality_group_is_dropped(self) -> None:
        """实机案例：按 120 个门店分组、横轴 21 年，每系列只有一两个点，整图近乎空白。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": f"{2016 + index % 21}-01", "store_name": f"店-{index:05d}", "net_sales": index}
            for index in range(120)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", group_field="store_name", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert：不分组时只有一条"总体"系列，且点数与横轴一致
        assert len(option["series"]) == 1
        assert len(option["series"][0]["data"]) == len(option["xAxis"]["data"])
        assert charts._MAX_GROUP_CARDINALITY < 120

    # 方法作用：验证分组过大但存在低基数候选时，自动改用候选维度。
    def test_huge_group_switches_to_low_cardinality_candidate(self) -> None:
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {
                "year_month": f"{2019 + index % 4}-01",
                "store_name": f"店-{index:05d}",
                "region_code": f"CN-{index % 5}",
                "net_sales": index,
            }
            for index in range(120)
        ]
        spec = ChartSpec(
            chart_type="line",
            x_field="year_month",
            group_field="store_name",
            series_fields=["net_sales"],
            switchable_fields=["region_code"],
        )

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert：改用 5 个地区分组，而不是 120 个门店
        assert len(option["series"]) == 5
        assert all("地区编码" in str(item["name"]) for item in option["series"])


class TestChartFallback:
    """覆盖模型不可用时的确定性兜底，不得回退到"永远 bar"。"""

    # 方法作用：验证时间列 + 数值列的兜底类型是折线而不是柱状。
    def test_fallback_infers_line_for_time_series(self) -> None:
        # Arrange
        from src.reporting.charts import fallback_chart_plan

        rows = [{"year_month": f"2016-{month:02d}", "net_sales": month} for month in range(1, 7)]

        # Act
        plan = fallback_chart_plan(question="按年月统计净销售额", columns=["year_month", "net_sales"], rows=rows)

        # Assert
        assert plan.charts[0].chart_type == "line"
        assert plan.charts[0].x_field == "year_month"
        assert plan.charts[0].series_fields == ["net_sales"]

    # 方法作用：验证分类列 + 数值列的兜底类型是柱状。
    def test_fallback_infers_bar_for_categories(self) -> None:
        # Arrange
        from src.reporting.charts import fallback_chart_plan

        rows = [{"region": f"区域{index}", "net_sales": index} for index in range(20)]

        # Act
        plan = fallback_chart_plan(question="按地区统计净销售额", columns=["region", "net_sales"], rows=rows)

        # Assert
        assert plan.charts[0].chart_type == "bar"

    # 方法作用：验证无数据时给出 table 方案而不是抛错。
    def test_fallback_without_rows_is_table(self) -> None:
        from src.reporting.charts import fallback_chart_plan

        assert fallback_chart_plan(question="空单元", columns=[], rows=[]).charts[0].chart_type == "table"

    # 方法作用：验证类型推断复用对话路径的同一个函数对象，防止两套实现再次分叉。
    def test_type_inference_reuses_conversation_helper(self) -> None:
        import src.tools.chart_generator as generator

        assert callable(generator.classify_chart_type)
        assert not hasattr(generator, "_classify_chart_type")

    # 方法作用：验证决策异常时退回兜底而不是让整份报表失败。
    @pytest.mark.asyncio
    async def test_decision_error_falls_back(self, monkeypatch) -> None:
        # Arrange
        import src.reporting.charts as charts

        async def _boom(**kwargs):
            raise RuntimeError("模型不可用")

        monkeypatch.setattr(charts, "_invoke_chart_plan", _boom)

        # Act
        plan = await charts.plan_charts(question="按年月统计", columns=["year_month", "net_sales"], rows=_result().rows)

        # Assert
        assert plan.charts and plan.charts[0].chart_type == "line"


class TestChartPlanContract:
    """覆盖契约对模型越界的容错：展示用字段不得让整批决策被拒收。"""

    # 方法作用：验证超长的系列清单被裁剪而不是抛错。
    def test_over_long_series_list_is_clipped(self) -> None:
        # Arrange / Act
        spec = ChartSpec(chart_type="line", series_fields=[f"c{index}" for index in range(20)])

        # Assert
        assert len(spec.series_fields) == 10

    # 方法作用：验证超量的图表方案被裁剪而不是抛错。
    def test_over_many_charts_are_clipped(self) -> None:
        # Arrange / Act
        plan = ReportChartPlan(charts=[ChartSpec(chart_type="bar") for _ in range(20)])

        # Assert
        assert len(plan.charts) == 6

    # 方法作用：验证非法图表类型仍被拒绝（白名单不放开）。
    def test_unknown_chart_type_rejected(self) -> None:
        # Arrange
        from pydantic import ValidationError

        # Act / Assert
        with pytest.raises(ValidationError):
            ChartSpec(chart_type="candlestick")

    def test_default_chart_type_is_bar(self) -> None:
        assert ChartSpec().chart_type == "bar"


class TestChartColumnFallback:
    """覆盖模型给的列名与结果列对不上时的回退，以及英文别名的中文兜底。"""

    # 方法作用：验证声明的指标列全不存在时降级，而不是拿别的数值列顶替。
    def test_unknown_metric_degrades_instead_of_substituting(self) -> None:
        """实机事故：series_fields 写了 SQL 里不存在的列，图上却画出了另一个指标。

        「换一个指标画」和「画不出来」在页面上长得一样，用户读到的却是错误结论，
        因此这里必须显式降级并让调用方披露原因。
        """
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 10},
            {"year_month": "2016-02", "region_code": "CN-BJ", "net_sales": 30},
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["nonexistent_metric"])

        # Act / Assert
        with pytest.raises(ChartNotRenderable):
            build_option_for_spec(spec, rows)

    # 方法作用：验证 x 轴列不存在于结果时回退到真实维度列，而不是退化成一个空刻度。
    def test_unknown_x_field_falls_back_to_dimension(self) -> None:
        """实机事故：x_field 对不上结果列，x 轴只剩下一个空字符串。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 10},
            {"year_month": "2016-02", "region_code": "CN-GD", "net_sales": 30},
        ]
        spec = ChartSpec(chart_type="bar", x_field="store_name", series_fields=["net_sales"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["xAxis"]["data"]
        assert "" not in option["xAxis"]["data"]

    # 方法作用：验证列注释缺失时用确定性词表把英文别名翻成中文。
    def test_english_alias_is_translated_deterministically(self) -> None:
        """图例出现 customer_count 这类英文别名是实机投诉点，翻译必须由代码完成。"""
        # Arrange
        from src.reporting.charts import translate_column_name

        # Act / Assert：整词命中
        assert translate_column_name("avg_order_value") == "客单价"
        assert translate_column_name("net_sales_amount") == "净销售额"
        # 分词命中
        assert translate_column_name("low_recency_pct") == "低近度占比"
        assert translate_column_name("customer_count") == "客户数"
        # 连词表都没命中时保留原名，不编造译名
        assert translate_column_name("xyzzy_field") == "xyzzy_field"
        # 含中文的列名不参与翻译
        assert translate_column_name("地区编码") == "地区编码"

    # 方法作用：验证列名对不上时切换分组仍能算出系列，前端切换不会清空数据。
    def test_group_options_stay_populated_on_column_mismatch(self) -> None:
        """「点了标签数据清空」的另一半来源是切换视图整批为空。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "net_sales": 10},
            {"year_month": "2016-01", "region_code": "CN-GD", "net_sales": 20},
        ]
        spec = ChartSpec(
            chart_type="bar",
            x_field="year_month",
            series_fields=["net_sales"],
            switchable_fields=["missing_dimension", "region_code"],
        )

        # Act
        _group_field, default_series, options = build_group_options(spec=spec, rows=rows)

        # Assert：有系列可画，且结果里不存在的候选维度不生成切换项
        assert default_series
        assert options
        assert all(series for series in options.values())
        assert "missing_dimension" not in options


class TestTableBlockIdUniqueness:
    """覆盖同一单元多张 table 方案时的块 ID 唯一性（实机 run_id=75814587 事故）。"""

    # 方法作用：验证同一单元两张 table 方案产出的块 ID 唯一，且不触发页面校验。
    # Args: 无（依赖模块级 _plan / _result 构造的单单元输入）。
    # Returns: 无返回值；断言失败即抛错。
    def test_two_table_plans_do_not_collide(self) -> None:
        """实机事故：chart_type=table 分支恒定用 table_{section_id}，同单元两张 table 必然重
        复 ID，ReportDocument 校验「同一区段页面块 ID 不能重复」直接让整份报表落盘失败。"""
        # Arrange
        from src.reporting.document import build_report_document

        chart_plans = {
            "u1": ReportChartPlan(charts=[
                ChartSpec(chart_type="table", title="分层规模"),
                ChartSpec(chart_type="table", title="贡献拆解"),
            ])
        }

        # Act
        artifact = build_report_document(_plan(), [_result()], chart_plans=chart_plans)

        # Assert：两张 table 块都保留，且 ID 互不相同
        table_blocks = [
            block
            for section in artifact.document.sections
            for block in section.blocks
            if block.type == "table"
        ]
        assert len(table_blocks) == 2
        assert len({block.id for block in table_blocks}) == 2
        # 首张沿用兼容 ID，避免既有钻取 source_block_id 指向失效
        assert table_blocks[0].id == "table_u1"
        assert table_blocks[1].id == "table_u1_1"


class TestChartIdentifierColumns:
    """覆盖功能 23.41：ID/编码/键列即便取值是数字也必须当维度。

    实机取证（分享 token 2gwR2fiF…，report d5e60e90…）：`store_id` / `product_id`
    的数据库类型是数值，`_split_columns` 按「取值全是数字」把它们判成指标列，
    于是横轴退化成 `['']`（price_promotion、procurement_inventory）或退成
    `['missed','summary']`（target_achievement），而系列仍按 year_month 生成。
    """

    # 方法作用：验证 ID/编码/键命名的列一律归入维度列。
    # Args: self - 测试实例；column - 待验证的列名。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "column",
        ["store_id", "product_id", "region_code", "channel_code", "batch_key", "trace_uuid"],
    )
    def test_identifier_columns_are_dimensions(self, column: str) -> None:
        """标识列是实体的名字，当指标画会让横轴失去类目。"""
        # Arrange
        from src.reporting.charts import _split_columns

        rows = [{"year_month": "2026-01", column: 1001}, {"year_month": "2026-02", column: 1002}]

        # Act
        numeric, dimensions = _split_columns(rows)

        # Assert
        assert column in dimensions
        assert column not in numeric

    # 方法作用：验证真正的数值指标列不会被标识列规则误伤。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_metric_columns_stay_numeric(self) -> None:
        """销售额、订单数、达成率仍是可加/可平均的指标列。"""
        # Arrange
        from src.reporting.charts import _split_columns

        rows = [{"net_sales": 10, "order_count": 2, "achieve_rate": 80.0}]

        # Act
        numeric, dimensions = _split_columns(rows)

        # Assert
        assert numeric == ["net_sales", "order_count", "achieve_rate"]
        assert dimensions == []

    # 方法作用：验证数值型 ID 列声明的横轴不再被丢掉换成时间列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_numeric_id_column_becomes_x_axis(self) -> None:
        """target_achievement 实机症状：横轴退成 ['missed','summary']，系列却按 year_month 出 8 条。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"store_id": store, "year_month": month, "net_sales": store % 10 + int(month[-2:])}
            for store in (1001, 1002, 1003)
            for month in ("2026-01", "2026-02")
        ]
        spec = ChartSpec(
            chart_type="bar",
            x_field="store_id",
            group_field="year_month",
            series_fields=["net_sales"],
        )

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert：横轴是门店，系列按年月拆成 2 条
        assert option["xAxis"]["data"] == ["1001", "1002", "1003"]
        assert len(option["series"]) == 2
        assert all(len(item["data"]) == 3 for item in option["series"])

    # 方法作用：验证结果里只有数值 ID 列时横轴仍有真实类目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_numeric_id_axis_is_not_a_blank_category(self) -> None:
        """procurement_inventory 实机症状：横轴只剩一个空字符串，图表几乎空白。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"product_id": product, "amount": product / 10} for product in (2001, 2002, 2003)]
        spec = ChartSpec(chart_type="bar", x_field="product_id", series_fields=["amount"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["xAxis"]["data"] == ["2001", "2002", "2003"]
        assert "" not in option["xAxis"]["data"]


class TestChartEmptyCategoryAxis:
    """覆盖功能 23.41：横轴全空时必须换列或降级，绝不把空字符串当类目。

    实机取证：price_promotion 的 `region_code` 实际返回全 NULL，两张 bar 图
    横轴都是 `['']`，页面上是两个没有任何类目的空框。
    """

    # 方法作用：验证声明的横轴列全空时优先改用有值的维度列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_null_x_falls_back_to_populated_dimension(self) -> None:
        """全空列不得成为横轴：它会吃掉所有数据点，只剩一个空刻度。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"region_code": None, "margin_alert_level": "high", "product_id": 2001, "amount": 10},
            {"region_code": None, "margin_alert_level": "low", "product_id": 2002, "amount": 20},
        ]
        spec = ChartSpec(
            chart_type="bar",
            x_field="region_code",
            series_fields=["amount"],
            switchable_fields=["margin_alert_level"],
        )

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["xAxis"]["data"] == ["high", "low"]
        assert "" not in option["xAxis"]["data"]
        assert option["series"][0]["data"] == [10.0, 20.0]

    # 方法作用：验证空字符串与 NULL 混排的横轴同样不被承认为类目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_blank_and_null_x_are_not_categories(self) -> None:
        """没有任何可用维度时明确报错，而不是画一张空框。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"region_code": None, "amount": 10}, {"region_code": "", "amount": 20}]
        spec = ChartSpec(chart_type="bar", x_field="region_code", series_fields=["amount"])

        # Act / Assert
        with pytest.raises(ValueError):
            build_option_for_spec(spec, rows)

    # 方法作用：验证横轴无类目时报表降级为明细表并留下可见说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_blank_axis_degrades_to_table_with_warning(self) -> None:
        """降级不能静默：页面 quality 块必须写明图表未能生成。"""
        # Arrange
        from src.reporting.document import build_report_document

        unit = UnitResult(
            unit_id="u1",
            columns=["region_code", "amount"],
            rows=[{"region_code": None, "amount": 10}],
        )
        plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="u1", question="按区域统计金额")])
        chart_plan = ReportChartPlan(charts=[ChartSpec(
            chart_type="bar",
            x_field="region_code",
            series_fields=["amount"],
        )])

        # Act
        artifact = build_report_document(plan, [unit], chart_plans={"u1": chart_plan})

        # Assert
        blocks = [block for section in artifact.document.sections for block in section.blocks]
        assert not [block for block in blocks if block.type == "chart"]
        limitations = [
            item
            for block in blocks
            if block.type == "quality"
            for item in block.content.get("limitations", [])
        ]
        assert any("降级为明细表" in item for item in limitations)

    # 方法作用：验证全空的分组维度不生成切换项。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_empty_group_option_is_not_offered(self) -> None:
        """切换项指向全空维度时，切过去只会得到空图：干脆不给这个入口。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"month": "2026-01", "region": "华东", "blank_dim": None, "amount": 10},
            {"month": "2026-02", "region": "华南", "blank_dim": None, "amount": 20},
        ]
        spec = ChartSpec(
            chart_type="line",
            x_field="month",
            group_field="region",
            series_fields=["amount"],
            switchable_fields=["blank_dim", "region"],
        )

        # Act
        _group_field, _series, options = build_group_options(spec=spec, rows=rows)

        # Assert
        assert "blank_dim" not in options


class TestChartTooltipTrigger:
    """覆盖功能 23.41：柱状图悬停只看单柱，不被整批同轴系列淹没。

    实机取证：所有 line/bar option 的 `tooltip.trigger` 都是 `axis`，
    悬停任意一根柱子会弹出该横轴上全部 8 条系列的读数。
    """

    # 方法作用：验证柱状图默认按数据项触发提示。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_bar_uses_item_tooltip(self) -> None:
        """悬停单柱只显示该系列/该维度的读数。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"region": "华东", "amount": 10}, {"region": "华南", "amount": 20}]
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=["amount"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["tooltip"]["trigger"] == "item"

    # 方法作用：验证折线图保留按轴触发提示。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_line_keeps_axis_tooltip(self) -> None:
        """趋势图的读法就是「看某一期的全部系列」，按轴触发才是对的。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2026-01", "region": "华东", "amount": 10},
            {"year_month": "2026-02", "region": "华东", "amount": 20},
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["amount"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["tooltip"]["trigger"] == "axis"

    # 方法作用：验证饼图与散点图保持按数据项触发。
    # Args: self - 测试实例；chart_type - 待验证图表类型。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("chart_type", ["pie", "scatter"])
    def test_special_types_keep_item_tooltip(self, chart_type: str) -> None:
        """饼图扇形与散点本身就只能逐项读，不得退回轴触发。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        if chart_type == "scatter":
            rows = [{"price": 10, "quantity": 2}, {"price": 20, "quantity": 5}]
            spec = ChartSpec(chart_type=chart_type, x_field="price", series_fields=["quantity"])
        else:
            rows = [{"region": "华东", "amount": 10}, {"region": "华南", "amount": 20}]
            spec = ChartSpec(chart_type=chart_type, x_field="region", series_fields=["amount"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        assert option["tooltip"]["trigger"] == "item"


class TestChartMultiMetricGroupSeries:
    """覆盖功能 23.41：声明了多个指标且带分组时，不能只画第一个指标。

    实机取证：ChartSpec 的 series_fields 声明了多个指标，带 group_field 时
    build_group_options 只取 metrics[0]，其余指标被静默丢掉。
    """

    # 方法作用：验证「指标 × 分组」逐对成系列，且系列名同时含指标名与分组取值。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_multi_metric_with_group_draws_every_metric(self) -> None:
        """只画第一个指标会让用户以为"另一个指标没有数据"。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2026-01", "region": "华东", "net_sales": 10, "order_count": 2},
            {"year_month": "2026-01", "region": "华南", "net_sales": 20, "order_count": 4},
            {"year_month": "2026-02", "region": "华东", "net_sales": 30, "order_count": 6},
            {"year_month": "2026-02", "region": "华南", "net_sales": 40, "order_count": 8},
        ]
        spec = ChartSpec(
            chart_type="bar",
            x_field="year_month",
            group_field="region",
            series_fields=["net_sales", "order_count"],
        )

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        names = [item["name"] for item in option["series"]]
        assert len(names) == 4
        assert "净销售额 - 地区：华东" in names
        assert "订单数 - 地区：华南" in names
        # 每个系列的数据长度必须等于横轴长度，否则前端切换分组会错位
        assert all(len(item["data"]) == len(option["xAxis"]["data"]) for item in option["series"])

    # 方法作用：验证系列数超限时按指标轮流取，每个指标都还在图上。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_series_limit_keeps_every_metric(self) -> None:
        """第一个指标吃满 8 条配额，等于又回到了"只画第一个指标"。"""
        # Arrange
        from src.reporting.charts import _MAX_SERIES, build_option_for_spec

        rows = [
            {"month": "2026-01", "region": f"R{index}", "net_sales": 10 + index, "order_count": 20 + index}
            for index in range(6)
        ]
        spec = ChartSpec(
            chart_type="bar",
            x_field="month",
            group_field="region",
            series_fields=["net_sales", "order_count"],
        )

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert
        names = [item["name"] for item in option["series"]]
        assert len(names) == _MAX_SERIES
        assert any(name.startswith("净销售额") for name in names)
        assert any(name.startswith("订单数") for name in names)


class TestChartMetricResolution:
    """覆盖功能 23.41：区分 requested / resolved，缺失指标必须披露而不是顶替。"""

    # 方法作用：验证部分指标不存在时只画存在的那个，并把缺失列写进警告。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_partial_metric_missing_keeps_existing_and_reports_missing(self) -> None:
        """缺哪一列要说清楚，否则用户会把"没画"当成"没数据"。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"month": "2026-01", "net_sales": 10}, {"month": "2026-02", "net_sales": 20}]
        spec = ChartSpec(chart_type="line", x_field="month", series_fields=["net_sales", "missing_metric"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert [item["name"] for item in option["series"]] == ["净销售额"]
        assert any("missing_metric" in item for item in warnings)

    # 方法作用：验证全部请求指标不存在时降级为明细表并留下可见说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_metrics_missing_degrades_to_table_with_warning(self) -> None:
        """不许用无关的数值列顶替指标：那会画出一张"长得像但不是它"的图。"""
        # Arrange
        from src.reporting.document import build_report_document

        unit = UnitResult(
            unit_id="u1",
            columns=["month", "net_sales"],
            rows=[{"month": "2026-01", "net_sales": 10}],
        )
        plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="u1", question="按月统计净销售额")])
        chart_plan = ReportChartPlan(charts=[ChartSpec(
            chart_type="bar",
            x_field="month",
            series_fields=["missing_metric"],
        )])

        # Act
        artifact = build_report_document(plan, [unit], chart_plans={"u1": chart_plan})

        # Assert
        blocks = [block for section in artifact.document.sections for block in section.blocks]
        assert not [block for block in blocks if block.type == "chart"]
        assert [block for block in blocks if block.type == "table"]
        limitations = [
            item
            for block in blocks
            if block.type == "quality"
            for item in block.content.get("limitations", [])
        ]
        assert any("missing_metric" in item for item in limitations)
        assert any("降级为明细表" in item for item in limitations)


class TestChartLateColumnDetection:
    """覆盖本轮回归：列识别只看前 50 行，后段才出现的列与指标整列消失。

    实机取证（分享 token 2gwR2fiF…，report d5e60e90… version 3）：持久化 manifest 的
    前 200 行里 `budget_tax.execution_rate_pct` / `hr_cost.engagement_response_count`
    全为空，真实读数出现在 200 行之后；`_COLUMN_SCAN_ROWS=50` 只扫前 50 行，
    把这些真实存在的指标判成维度/不存在，重建时直接抛 `ChartNotRenderable`。
    """

    # 方法作用：验证前 50 行不出现的列键在全量扫描后仍被发现。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_column_key_appearing_late_is_discovered(self) -> None:
        """`columns` 只从前 50 行取时，后段才出现的键会彻底消失。"""
        # Arrange
        from src.reporting.charts import _split_columns

        rows: list[dict] = [{"month": "2026-01", "net_sales": index} for index in range(80)]
        rows.append({"month": "2026-02", "net_sales": 1, "late_amount": Decimal("3.5")})

        # Act
        numeric, _dimensions = _split_columns(rows)

        # Assert
        assert "late_amount" in numeric

    # 方法作用：验证前 200 行全空、后段才出现 Decimal 的指标列被识别为数值列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_metric_blank_for_first_two_hundred_rows_is_numeric(self) -> None:
        """真实形状：同一结果集里 union 分支的指标出现在 200 行之后。"""
        # Arrange
        from src.reporting.charts import _split_columns

        rows: list[dict] = [
            {"record_type": "budget", "month": f"2026-{month:02d}", "execution_rate_pct": None}
            for month in range(1, 13)
        ] * 30
        rows += [
            {"record_type": "tax", "month": f"2026-{month:02d}", "execution_rate_pct": Decimal("10.5")}
            for month in range(1, 13)
        ] * 30

        # Act
        numeric, dimensions = _split_columns(rows)

        # Assert
        assert "execution_rate_pct" in numeric
        assert "execution_rate_pct" not in dimensions

    # 方法作用：验证后段才出现的指标能在图上画出真实读数，而不是被判成不存在的列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_late_metric_is_drawn_with_tail_values(self) -> None:
        """声明指标若被判成「结果里不存在」，整张图会降级为明细表。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows: list[dict] = [
            {"record_type": "budget", "month": "2026-01", "execution_rate_pct": None}
            for _ in range(60)
        ]
        rows.append({"record_type": "tax", "month": "2026-01", "execution_rate_pct": Decimal("10.5")})
        spec = ChartSpec(chart_type="bar", x_field="record_type", series_fields=["execution_rate_pct"])

        # Act
        option = build_option_for_spec(spec, rows)

        # Assert：只有 tax 分支有读数，budget 分支留空而不是整图消失
        assert option["xAxis"]["data"] == ["budget", "tax"]
        assert option["series"][0]["data"] == [None, 10.5]

    # 方法作用：验证后段数值列夹带少量脏值时仍按数值列处理。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_late_numeric_column_tolerates_few_dirty_values(self) -> None:
        """合计行的 N/A 不能让整列降级：这是 2026-09-15 修过的旧 bug，不得回归。"""
        # Arrange
        from src.reporting.charts import _split_columns

        rows: list[dict] = [{"month": "2026-01", "amount": Decimal("10")} for _ in range(80)]
        rows += [{"month": "合计", "amount": "N/A"}, {"month": "合计", "amount": ""}]
        rows.append({"month": "2026-02", "amount": "1,234.00"})

        # Act
        numeric, _dimensions = _split_columns(rows)

        # Assert
        assert "amount" in numeric

    # 方法作用：验证全量扫描后全空列仍归维度，不会被当成指标。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_null_column_stays_dimension_after_full_scan(self) -> None:
        """全空列当指标会让横轴失去类目；扫全量不得改变这一分类结论。"""
        # Arrange
        from src.reporting.charts import _split_columns

        rows: list[dict] = [{"region_code": None, "amount": index} for index in range(300)]

        # Act
        numeric, dimensions = _split_columns(rows)

        # Assert
        assert "region_code" in dimensions
        assert "region_code" not in numeric
        assert "amount" in numeric


class TestChartHighCardinalityCategories:
    """覆盖本轮回归：声明合法的横轴列基数过大时，页面输出上百根无法辨认的柱子。

    实机取证（report d5e60e90… version 3 的 manifest 重建）：`center_name` 198 个类目、
    `product_id` 200 个类目、`experiment_id` 191 个类目；页面宽约 800px，
    `axisLabel.hideOverlap=true` 会隐藏大部分标签——用户看到的就是「图里缺了什么」。
    """

    # 方法作用：验证类目数超限时确定性裁剪到可读上限，且系列与轴严格对齐。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_many_categories_are_trimmed_to_readable_limit(self) -> None:
        """198~200 个类目挤在约 800px 宽的图里，必须裁到可读数量。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.charts import build_option_for_spec

        rows = [{"product_id": f"P{index:04d}", "net_sales": index + 1} for index in range(200)]
        spec = ChartSpec(chart_type="bar", x_field="product_id", series_fields=["net_sales"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert：轴长等于可读上限，系列与轴严格对齐，并披露原始/保留数量
        assert len(option["xAxis"]["data"]) == charts._MAX_VISIBLE_CATEGORIES
        assert all(len(item["data"]) == charts._MAX_VISIBLE_CATEGORIES for item in option["series"])
        assert "P0199" in option["xAxis"]["data"]
        assert "P0000" not in option["xAxis"]["data"]
        assert any("200" in item and str(charts._MAX_VISIBLE_CATEGORIES) in item for item in warnings)

    # 方法作用：验证保留依据是所有可见系列的绝对量级综合，而不是行序。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_selection_is_by_magnitude_not_row_order(self) -> None:
        """简单取前 N 行会把排在最后的高量级类目裁掉。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.charts import build_option_for_spec

        rows: list[dict] = []
        for index in range(30):
            rows.append({"region": f"R{index:02d}", "channel": "A", "amount": index + 1})
            rows.append({"region": f"R{index:02d}", "channel": "B", "amount": index + 1})
        rows.append({"region": "R99", "channel": "A", "amount": 5000})
        spec = ChartSpec(
            chart_type="bar",
            x_field="region",
            group_field="channel",
            series_fields=["amount"],
            switchable_fields=["channel"],
        )

        # Act
        option = build_option_for_spec(spec, rows, warnings=[])

        # Assert
        assert len(option["xAxis"]["data"]) == charts._MAX_VISIBLE_CATEGORIES
        assert "R99" in option["xAxis"]["data"]
        assert "R00" not in option["xAxis"]["data"]

    # 方法作用：验证负数读数按绝对量级参与保留判定。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_negative_magnitudes_use_absolute_value(self) -> None:
        """亏损类指标的绝对量级大，不能被符号抵消掉。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.charts import build_option_for_spec

        rows: list[dict] = [
            {"region": f"R{index:02d}", "amount": -(index + 1) * 10} for index in range(30)
        ]
        rows.append({"region": "R99", "amount": -9999})
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=["amount"])

        # Act
        option = build_option_for_spec(spec, rows, warnings=[])

        # Assert
        assert "R99" in option["xAxis"]["data"]
        assert "R00" not in option["xAxis"]["data"]
        assert len(option["xAxis"]["data"]) == charts._MAX_VISIBLE_CATEGORIES

    # 方法作用：验证并列量级下的保留结果稳定且确定。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tie_breaks_are_stable_and_deterministic(self) -> None:
        """相同输入必须给出相同输出，并列时按原始顺序优先。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.charts import build_option_for_spec

        rows = [{"region": f"R{index:02d}", "amount": 5} for index in range(40)]
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=["amount"])

        # Act
        first = build_option_for_spec(spec, rows, warnings=[])["xAxis"]["data"]
        second = build_option_for_spec(spec, rows, warnings=[])["xAxis"]["data"]

        # Assert
        assert first == second
        assert first == [f"R{index:02d}" for index in range(charts._MAX_VISIBLE_CATEGORIES)]

    # 方法作用：验证默认视图与每个切换分组视图使用同一套保留类目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_group_options_share_the_same_kept_categories(self) -> None:
        """切换分组后 data 长度错位，是「只修 option 忘了 group_options」的典型症状。"""
        # Arrange
        import src.reporting.charts as charts
        from src.reporting.charts import build_group_options, build_option_for_spec

        rows: list[dict] = []
        for index in range(40):
            for channel in ("A", "B"):
                rows.append({"region": f"R{index:02d}", "channel": channel, "amount": index + 1})
        spec = ChartSpec(
            chart_type="bar",
            x_field="region",
            group_field="channel",
            series_fields=["amount"],
            switchable_fields=["channel"],
        )

        # Act
        _group, default_series, options = build_group_options(spec=spec, rows=rows, warnings=[])
        option = build_option_for_spec(spec, rows, warnings=[])

        # Assert：所有视图与默认 option 的横轴长度完全一致
        assert {len(item["data"]) for item in default_series} == {charts._MAX_VISIBLE_CATEGORIES}
        assert {len(item["data"]) for series in options.values() for item in series} == {
            charts._MAX_VISIBLE_CATEGORIES
        }
        assert len(option["xAxis"]["data"]) == charts._MAX_VISIBLE_CATEGORIES

    # 方法作用：验证散点图的数值轴不受类目上限影响。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_scatter_numeric_axis_is_not_trimmed(self) -> None:
        """散点图的两条轴都是数值轴，按类目截断会让点集凭空消失。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"price": index * 1.5, "quantity": index} for index in range(200)]
        spec = ChartSpec(chart_type="scatter", x_field="price", series_fields=["quantity"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert len(option["series"][0]["data"]) == 200
        assert warnings == []

    # 方法作用：验证时间轴只做粒度上卷，不按量级重排也不裁剪。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_time_axis_keeps_time_order_after_rollup(self) -> None:
        """把时间趋势按量级重排会得出完全错误的结论。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": f"{2016 + index // 12}-{index % 12 + 1:02d}", "amount": 100 - index}
            for index in range(30)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["amount"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert：上卷到年并保持时间顺序，且不产生裁剪披露
        assert option["xAxis"]["data"] == ["2016", "2017", "2018"]
        assert warnings == []

    # 方法作用：验证量级全为零、无法可靠排序时抛错降级而不是随意截断。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_zero_magnitude_categories_degrade_instead_of_guessing(self) -> None:
        """没有任何非零读数时，保留哪 24 个纯属随机，必须降级为明细表。"""
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [{"region": f"R{index:03d}", "amount": 0} for index in range(60)]
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=["amount"])

        # Act / Assert
        with pytest.raises(ChartNotRenderable):
            build_option_for_spec(spec, rows, warnings=[])


class TestChartAxisFallbackDisclosure:
    """覆盖本轮回归：声明横轴不可用时静默换列，页面 quality 块里没有任何说明。

    实机取证：`chart_price_promotion_1` 标题「各区域折扣深度对比」、声明 `region_code`，
    该列全 NULL，代码回退到 `margin_alert_level`，轴变成 negative_margin/low_margin，
    而 warnings 仍为空——用户读到的是「各区域」，看到的却是告警等级。
    """

    # 方法作用：验证声明横轴全空而改用其它列时，warnings 写明声明列与实际列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_blank_declared_axis_fallback_is_disclosed(self) -> None:
        """回退可以保住可视化，但不得无痕换维度。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"region_code": None, "margin_alert_level": "high", "amount": 10},
            {"region_code": None, "margin_alert_level": "low", "amount": 20},
        ]
        spec = ChartSpec(chart_type="bar", x_field="region_code", series_fields=["amount"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert option["xAxis"]["data"] == ["high", "low"]
        assert any("region_code" in item and "margin_alert_level" in item for item in warnings)

    # 方法作用：验证声明横轴列在结果里不存在时同样披露。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_declared_axis_fallback_is_disclosed(self) -> None:
        """列名对不上结果列时换了轴，页面必须说清楚。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year_month": "2016-01", "region_code": "CN-BJ", "amount": 10},
            {"year_month": "2016-02", "region_code": "CN-GD", "amount": 20},
        ]
        spec = ChartSpec(chart_type="bar", x_field="store_name", series_fields=["amount"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert option["xAxis"]["data"]
        assert any("store_name" in item for item in warnings)

    # 方法作用：验证显式标题在横轴换列后失真时被一并披露。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_title_risk_is_disclosed_on_axis_change(self) -> None:
        """「各区域折扣深度对比」配上告警等级轴，必须让用户看到这层错位。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"region_code": None, "margin_alert_level": "negative_margin", "amount": 10},
            {"region_code": None, "margin_alert_level": "low_margin", "amount": 20},
        ]
        spec = ChartSpec(
            chart_type="bar",
            title="各区域折扣深度对比",
            x_field="region_code",
            series_fields=["amount"],
        )
        warnings: list[str] = []

        # Act
        build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert any("各区域折扣深度对比" in item for item in warnings)

    # 方法作用：验证正常使用声明横轴时不产生任何横轴相关 warning。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_declared_axis_in_use_produces_no_warning(self) -> None:
        """正常路径不得无中生有地报警，否则用户会忽略真正的警告。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"region_code": "CN-BJ", "amount": 10},
            {"region_code": "CN-GD", "amount": 20},
        ]
        spec = ChartSpec(chart_type="bar", x_field="region_code", series_fields=["amount"])
        warnings: list[str] = []

        # Act
        build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert warnings == []

    # 方法作用：验证散点图数值轴正常路径不误报横轴回退。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_scatter_axis_produces_no_warning(self) -> None:
        """散点图的 x_field 本来就是数值列，不能按维度规则判成「类型不适用」。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [{"price": 10, "quantity": 2}, {"price": 20, "quantity": 5}]
        spec = ChartSpec(chart_type="scatter", x_field="price", series_fields=["quantity"])
        warnings: list[str] = []

        # Act
        build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert warnings == []

    # 方法作用：验证声明列存在但类型不适合作横轴时，同样披露且文案不谎报原因。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_type_mismatched_declared_axis_is_disclosed(self) -> None:
        """数值列当分类图的横轴属「类型不适用」，不能说成「列不存在或整列为空」。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"year": "2016", "region": "华东", "amount": 10},
            {"year": "2017", "region": "华南", "amount": 20},
        ]
        spec = ChartSpec(chart_type="bar", x_field="year", series_fields=["amount"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert：换了轴并披露，且原因没有说成「不存在」
        assert option["xAxis"]["data"] == ["华东", "华南"]
        assert any("year" in item and "region" in item for item in warnings)
        assert not any("列不存在或整列为空" in item for item in warnings)

    # 方法作用：验证连替代列都没有时，降级说明里仍点名声明的是哪一列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unavailable_axis_without_alternative_names_declared_column(self) -> None:
        """只说「横轴没有可用类目」，用户不知道是哪一列被判定不可用。"""
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [{"region_code": None, "amount": 10}]
        spec = ChartSpec(chart_type="bar", x_field="region_code", series_fields=["amount"])
        warnings: list[str] = []

        # Act / Assert
        with pytest.raises(ChartNotRenderable):
            build_option_for_spec(spec, rows, warnings=warnings)
        assert any("region_code" in item for item in warnings)

    # 方法作用：验证回退披露经 build_report_document 进入所在区段的 quality 块。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_axis_fallback_warning_reaches_quality_block(self) -> None:
        """只停在 helper 参数或日志里的披露，用户一个字也看不到。"""
        # Arrange
        from src.reporting.document import build_report_document

        unit = UnitResult(
            unit_id="u1",
            columns=["region_code", "margin_alert_level", "amount"],
            rows=[
                {"region_code": None, "margin_alert_level": "high", "amount": 10},
                {"region_code": None, "margin_alert_level": "low", "amount": 20},
            ],
        )
        plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="u1", question="按区域统计折扣金额")])
        chart_plan = ReportChartPlan(charts=[ChartSpec(
            chart_type="bar",
            title="各区域折扣深度对比",
            x_field="region_code",
            series_fields=["amount"],
        )])

        # Act
        artifact = build_report_document(plan, [unit], chart_plans={"u1": chart_plan})

        # Assert
        limitations = [
            item
            for section in artifact.document.sections
            for block in section.blocks
            if block.type == "quality"
            for item in block.content.get("limitations", [])
        ]
        assert any("region_code" in item and "margin_alert_level" in item for item in limitations)


class TestChartComparisonViewsOnlyForTimeAxis:
    """覆盖功能 23.41 回归：同比/环比只能挂在已识别的时间轴上。

    实机取证（分享 token 2gwR2fiF…，version 3）：13 张非时间图的 group_options 里带
    `__mom__` 切换项，横轴分别是 region_code / store_id / rfm_segment / aging_bucket /
    experiment_id —— 读数成了「相邻类别之间的变化率」，业务语义是错的。
    """

    # 方法作用：验证分类轴不生成环比视图。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_category_axis_has_no_comparison_views(self) -> None:
        """地区、产品、实验等分类轴上的「环比」是相邻类别之差，不是时间变化。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"region": region, "net_sales": value}
            for region, value in (("华东", 10), ("华南", 20), ("华北", 30))
        ]
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        assert "__mom__" not in options
        assert "__yoy__" not in options

    # 方法作用：验证月度时间轴仍然保留环比与同比。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_month_axis_keeps_comparison_views(self) -> None:
        """修复不得波及已识别的时间轴：月初到年末的环比与同比必须还在。"""
        # Arrange
        from src.reporting.charts import build_group_options

        # 13 个点才够算一整年同比（_yoy_periods("month") == 12，需严格多于）。
        rows = [
            {"year_month": f"{2016 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}", "net_sales": month}
            for month in range(1, 14)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        assert "__mom__" in options
        assert "__yoy__" in options

    # 方法作用：验证年度轴不出环比（与同比等价）但仍出同比。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_year_axis_keeps_yoy_only(self) -> None:
        """年粒度下「与上一期比」就是同比，再出一个同名视图只会重复。"""
        # Arrange
        from src.reporting.charts import build_group_options

        # 4 年共 48 个月：超过 _TIME_ROLLUP_THRESHOLD=24，轴自动上卷到 year 粒度。
        rows = [
            {"year_month": f"{2016 + index // 12}-{index % 12 + 1:02d}", "net_sales": 100 + index}
            for index in range(48)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        assert "__mom__" not in options
        assert "__yoy__" in options

    # 方法作用：验证日粒度时间轴同样保留环比。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_day_axis_keeps_mom_view(self) -> None:
        """日粒度是已识别的时间轴，环比成立（只是不提供同比）。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {"stat_date": f"2016-01-{day:02d}", "net_sales": day * 10}
            for day in range(1, 6)
        ]
        spec = ChartSpec(chart_type="line", x_field="stat_date", series_fields=["net_sales"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        assert "__mom__" in options


class TestChartScatterAxisExclusion:
    """覆盖功能 23.41 回归：散点图的横轴列被排除出 y 之后，不得被谎报为「不存在」。

    实机语义：`scatter(x_field=price, series_fields=[price, quantity])` 里 price 既当 x
    又出现在 series_fields，代码把 x 从 resolved 排除后，missing 又按「不在 resolved」
    判定，于是向用户披露「price 不存在」——那列明明就在结果里。
    """

    # 方法作用：验证 x 同时写进 series_fields 时不产生缺失披露。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_scatter_x_in_series_fields_is_not_reported_missing(self) -> None:
        """列存在但作 x 被排除，既不是 missing 也不能显示「不存在」。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec, translate_column_name

        rows = [{"price": index, "quantity": index * 2} for index in range(1, 6)]
        spec = ChartSpec(chart_type="scatter", x_field="price", series_fields=["price", "quantity"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert warnings == []
        # y 轴必须落在另一个数值列上，不能拿 x 列自己当纵轴
        assert option["yAxis"]["name"] == translate_column_name("quantity")
        assert option["yAxis"]["name"] != option["xAxis"]["name"]
        assert len(option["series"][0]["data"]) == 5

    # 方法作用：验证 scatter 只声明 x 时确定性选其它数值列作 y，且不产生警告。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_scatter_with_only_x_declared_picks_other_numeric_column(self) -> None:
        """只把 x 写进 series_fields 是模型的常见写法，不能因此降级整张图。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec, translate_column_name

        rows = [{"price": index, "quantity": index * 2} for index in range(1, 6)]
        spec = ChartSpec(chart_type="scatter", x_field="price", series_fields=["price"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert warnings == []
        assert option["yAxis"]["name"] == translate_column_name("quantity")
        assert option["series"][0]["data"] == [[index, index * 2] for index in range(1, 6)]

    # 方法作用：验证确实没有其它数值列时按「缺少可用纵轴指标」降级。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_scatter_without_any_y_degrades_without_claiming_x_is_missing(self) -> None:
        """只有一个数值列时无 y 可画，但降级原因必须说纵轴，不能说 x 列不存在。"""
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [{"price": index} for index in range(1, 6)]
        spec = ChartSpec(chart_type="scatter", x_field="price", series_fields=["price"])
        warnings: list[str] = []

        # Act / Assert
        with pytest.raises(ChartNotRenderable) as excinfo:
            build_option_for_spec(spec, rows, warnings=warnings)
        message = str(excinfo.value)
        assert "纵轴" in message
        assert "不存在" not in message
        assert not any("price" in item and "不存在" in item for item in warnings)

    # 方法作用：验证非散点图的既有 requested/resolved 保护未被本轮改动放宽。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_scatter_declared_dimension_as_metric_still_degrades(self) -> None:
        """分类图的横轴列不可能同时是数值指标，原契约的降级保持不变。"""
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [{"region": "华东", "net_sales": 10}, {"region": "华南", "net_sales": 20}]
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=["region"])

        # Act / Assert
        with pytest.raises(ChartNotRenderable):
            build_option_for_spec(spec, rows)


class TestChartMetricSeriesWithoutGroup:
    """覆盖功能 23.41 回归：无分组的多指标同样要受单图系列上限约束。

    series_fields 的契约上限是 10（ChartSpec.max_length=10），而 `not group_field and
    len(metrics) > 1` 分支直接返回全部系列，_MAX_SERIES=8 在这一路径上完全失效。
    """

    # 方法作用：构造无分组的多指标图表方案与数据。
    # Args: metric_count - 指标个数。
    # Returns: (图表方案, 结果行)。
    def _metric_case(self, metric_count: int) -> tuple[ChartSpec, list[dict]]:
        metrics = [f"metric_{index:02d}" for index in range(metric_count)]
        rows = [
            {"region": f"R{index % 4}", **{metric: index + offset for offset, metric in enumerate(metrics)}}
            for index in range(8)
        ]
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=metrics)
        return spec, rows

    # 方法作用：验证 10 个指标被截到 _MAX_SERIES 并向用户披露。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_ten_metrics_are_capped_and_disclosed(self) -> None:
        """10 条系列会挤爆图例；截断必须说清楚，不能静默少画。"""
        # Arrange
        from src.reporting.charts import _MAX_SERIES, build_group_options

        spec, rows = self._metric_case(10)
        warnings: list[str] = []

        # Act
        _group, default_series, options = build_group_options(spec=spec, rows=rows, warnings=warnings)

        # Assert
        assert len(default_series) == _MAX_SERIES
        assert len(options[""]) == _MAX_SERIES
        assert any("10" in item and str(_MAX_SERIES) in item for item in warnings)

    # 方法作用：验证截断后系列顺序稳定且保留的是前 N 个指标。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_series_order_is_stable_after_capping(self) -> None:
        """同一份输入两次构建必须给出同一批系列，否则页面刷新会换一张图。"""
        # Arrange
        from src.reporting.charts import build_group_options

        spec, rows = self._metric_case(10)

        # Act
        _group, first, _options = build_group_options(spec=spec, rows=rows, warnings=[])
        _group, second, _options2 = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        first_names = [item["name"] for item in first]
        assert first_names == [item["name"] for item in second]
        assert first_names == [f"metric_{index:02d}" for index in range(8)]

    # 方法作用：验证 8 个以内指标不产生截断警告。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_eight_metrics_produce_no_cap_warning(self) -> None:
        """上限之内是正常路径，不该给用户报一条无意义的限制。"""
        # Arrange
        from src.reporting.charts import build_group_options

        spec, rows = self._metric_case(8)
        warnings: list[str] = []

        # Act
        _group, default_series, _options = build_group_options(spec=spec, rows=rows, warnings=warnings)

        # Assert
        assert len(default_series) == 8
        assert warnings == []


class TestChartGroupOptionsBuiltOnce:
    """覆盖性能回归：一次图表块构建不得对同一份输入算两遍分组透视。

    实机规模下单元可达数千行、多张图、多个候选分组，重复计算直接翻倍耗时与内存。
    """

    # 方法作用：构造带分组与候选切换维度的柱图结果行。
    # Args: month_count - 月份数。
    # Returns: 结果行列表。
    def _rows(self, month_count: int = 12) -> list[dict]:
        return [
            {
                "year_month": f"2016-{month:02d}",
                "region": f"R{month % 3}",
                "channel": f"C{month % 2}",
                "net_sales": month * 100.0,
            }
            for month in range(1, month_count + 1)
        ]

    # 方法作用：验证一次块构建只调用一次 build_group_options。
    # Args: self - 测试实例；monkeypatch - pytest 补丁夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_group_options_are_built_once_per_chart_block(self, monkeypatch) -> None:
        """build_option_for_spec 与 _build_chart_blocks 各算一遍，等于同一份透视跑两次。"""
        # Arrange
        import src.reporting.charts as charts
        import src.reporting.document as document

        calls: list[int] = []
        original = charts.build_group_options

        # 方法作用：记录调用次数后转交原实现。
        # Args: args - 位置参数；kwargs - 关键字参数。
        # Returns: 原实现的返回值。
        def _spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(charts, "build_group_options", _spy)
        monkeypatch.setattr(document, "build_group_options", _spy)
        plan = ReportChartPlan(charts=[ChartSpec(
            chart_type="bar",
            title="净销售额 对比",
            x_field="year_month",
            group_field="region",
            series_fields=["net_sales"],
            switchable_fields=["region", "channel"],
        )])

        # Act
        blocks = document._build_chart_blocks(
            section_id="u1",
            data_ref="data:u1",
            drilldown_id="dd1",
            plan=plan,
            rows=self._rows(),
            columns=["year_month", "region", "channel", "net_sales"],
        )

        # Assert
        assert len(calls) == 1, f"分组透视被重复计算 {len(calls)} 次"
        assert len(blocks) == 1
        content = blocks[0].content
        # 复用后分组切换视图的内容契约必须完全不变
        assert content["group_field"] == "region"
        assert "region" in content["group_options"]
        assert content["group_labels"]["__mom__"] == "环比(%)"

    # 方法作用：验证复用不往 ECharts option 里塞内部字段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reuse_does_not_leak_internal_fields_into_option(self) -> None:
        """option 根键有白名单校验，透出内部结构会被 ReportBlock 直接拒绝。"""
        # Arrange
        import src.reporting.document as document

        plan = ReportChartPlan(charts=[ChartSpec(
            chart_type="bar",
            x_field="year_month",
            group_field="region",
            series_fields=["net_sales"],
        )])

        # Act
        blocks = document._build_chart_blocks(
            section_id="u1",
            data_ref="data:u1",
            drilldown_id="dd1",
            plan=plan,
            rows=self._rows(),
            columns=["year_month", "region", "channel", "net_sales"],
        )

        # Assert
        assert set(blocks[0].echarts_option) <= {"xAxis", "yAxis", "series", "tooltip", "legend", "dataZoom", "visualMap"}


class TestChartUnavailableMetricReason:
    """覆盖功能 23.41 遗留缺陷：指标列不可用的原因必须区分「列不存在」与「整列为空」。

    `_split_columns` 已正确把全空列归入 dimensions，但 `_usable_columns` 仍以
    `name not in numeric` 作为 missing 判据，于是**列明明在结果里、只是整列 NULL 或空串**
    时，页面谎称「声明的指标列在结果中不存在」——用户会去查 SQL 别名，而真正的问题是没读数。
    """

    # 方法作用：验证全部声明指标列存在但整列全空时，降级原因说「不可用」而非「不存在」。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_metrics_present_but_empty_reports_unusable_not_absent(self) -> None:
        """列在结果里、只是整列没读数；说成「不存在」会让用户去排查一个不存在的别名问题。"""
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [
            {"month": "2026-01", "net_sales": None},
            {"month": "2026-02", "net_sales": None},
        ]
        spec = ChartSpec(chart_type="line", x_field="month", series_fields=["net_sales"])

        # Act
        with pytest.raises(ChartNotRenderable) as excinfo:
            build_option_for_spec(spec, rows)

        # Assert
        message = str(excinfo.value)
        assert "net_sales" in message
        assert "不存在" not in message
        assert "不可用" in message or "整列为空" in message

    # 方法作用：验证部分指标正常、另一指标列存在但整列空串时，警告如实说明原因。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_partially_empty_metric_reports_unusable_not_absent(self) -> None:
        """缺哪一列要说清楚，但原因不能说错：空串列并非不存在。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"month": "2026-01", "net_sales": 10, "blank_metric": ""},
            {"month": "2026-02", "net_sales": 20, "blank_metric": ""},
        ]
        spec = ChartSpec(chart_type="line", x_field="month", series_fields=["net_sales", "blank_metric"])
        warnings: list[str] = []

        # Act
        option = build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        assert len(option["series"]) == 1
        # 原因必须挂在列名后面：blank_metric 就在结果里，只能因整列为空被排除
        assert any("blank_metric（整列为空或无有效数值）" in item for item in warnings)
        assert not any("不存在" in item for item in warnings)

    # 方法作用：验证真正不存在的列仍被明确披露为「不存在」，降级行为不变。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_truly_absent_metric_still_disclosed_as_absent(self) -> None:
        """按 schema 猜的列名在 SQL 里被改了别名，这正是「不存在」要覆盖的场景。"""
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [{"month": "2026-01", "net_sales": 10}, {"month": "2026-02", "net_sales": 20}]
        spec = ChartSpec(chart_type="line", x_field="month", series_fields=["ghost_metric"])

        # Act
        with pytest.raises(ChartNotRenderable) as excinfo:
            build_option_for_spec(spec, rows)

        # Assert
        message = str(excinfo.value)
        assert "ghost_metric" in message
        assert "不存在" in message

    # 方法作用：验证混合情形下真正不存在的列仍在 warnings 里被点名。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mixed_absent_and_empty_columns_are_disclosed_separately(self) -> None:
        """一列不存在、一列存在却全空，两类原因都要落到用户可见说明里。"""
        # Arrange
        from src.reporting.charts import build_option_for_spec

        rows = [
            {"month": "2026-01", "net_sales": 10, "blank_metric": None},
            {"month": "2026-02", "net_sales": 20, "blank_metric": None},
        ]
        spec = ChartSpec(
            chart_type="line",
            x_field="month",
            series_fields=["net_sales", "blank_metric", "ghost_metric"],
        )
        warnings: list[str] = []

        # Act
        build_option_for_spec(spec, rows, warnings=warnings)

        # Assert
        # 两类原因各挂各的列：blank_metric 有值却全空、ghost_metric 压根不存在
        assert any("blank_metric（整列为空或无有效数值）" in item for item in warnings)
        assert any("ghost_metric（列不存在）" in item for item in warnings)

    # 方法作用：验证有值但类型不适合作指标的列，不得被说成「不存在」或「整列为空」。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_present_but_unusable_type_is_neither_absent_nor_empty(self) -> None:
        """横轴维度列被误当指标时有值、有类目：两种原因都是假话，必须另说。"""
        # Arrange
        from src.reporting.charts import ChartNotRenderable, build_option_for_spec

        rows = [{"region": "华东", "net_sales": 10}, {"region": "华南", "net_sales": 20}]
        spec = ChartSpec(chart_type="bar", x_field="region", series_fields=["region"])

        # Act
        with pytest.raises(ChartNotRenderable) as excinfo:
            build_option_for_spec(spec, rows)

        # Assert
        message = str(excinfo.value)
        assert "region" in message
        assert "不存在" not in message
        assert "整列为空" not in message


class TestChartSeriesMagnitudeRanking:
    """覆盖本轮回归：系列保留的排序依据必须是**各点绝对值合计**。"""

    # 方法作用：验证正负读数不会互相抵消掉整条系列的量级。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_opposite_signs_do_not_cancel_out(self) -> None:
        """+100/-100 的分组合计绝对值是 200，不能被算成 0 而输给 +2/+2 的分组。"""
        # Arrange
        from src.reporting.charts import _pivot_series

        rows = [
            {"year_month": "2026-01", "region": "volatile", "amount": 100},
            {"year_month": "2026-02", "region": "volatile", "amount": -100},
            {"year_month": "2026-01", "region": "tiny", "amount": 2},
            {"year_month": "2026-02", "region": "tiny", "amount": 2},
        ]

        # Act
        series = _pivot_series(
            rows=rows,
            x_field="year_month",
            group_field="region",
            metric="amount",
            labels=None,
            limit=1,
        )

        # Assert：保留波动大的那条，且它的两个点都还在
        assert [item["name"] for item in series] == ["地区：volatile"]
        assert series[0]["data"] == [100.0, -100.0]

    # 方法作用：验证量级并列时保留原始分组出现顺序，不做二次重排。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_ties_keep_original_group_order(self) -> None:
        """相同输入必须给出相同输出；并列时按分组首次出现序取前 N。"""
        # Arrange
        from src.reporting.charts import _pivot_series

        rows = [
            {"year_month": "2026-01", "region": f"R{index}", "amount": 5}
            for index in range(4)
        ]

        # Act
        series = _pivot_series(
            rows=rows,
            x_field="year_month",
            group_field="region",
            metric="amount",
            labels=None,
            limit=3,
        )

        # Assert
        assert [item["name"] for item in series] == ["地区：R0", "地区：R1", "地区：R2"]


class TestChartSeriesTrimDisclosure:
    """覆盖本轮回归：任何把系列数截到上限的路径都必须向用户披露事实。"""

    # 方法作用：验证单指标分组裁剪披露原系列数、保留数与未展示数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_single_metric_group_trim_is_disclosed(self) -> None:
        """12 个分组只画 8 条：用户必须知道少了 4 条，而不是以为只有 8 个分组。"""
        # Arrange
        from src.reporting.charts import _MAX_SERIES, build_group_options

        rows = [
            {"year_month": "2026-01", "region": f"R{index:02d}", "amount": index + 1}
            for index in range(12)
        ]
        spec = ChartSpec(
            chart_type="bar", x_field="year_month", group_field="region", series_fields=["amount"]
        )
        warnings: list[str] = []

        # Act
        _group, default_series, _options = build_group_options(spec=spec, rows=rows, warnings=warnings)

        # Assert
        assert len(default_series) == _MAX_SERIES
        assert len(warnings) == 1
        text = warnings[0]
        assert "12" in text and str(_MAX_SERIES) in text and "4" in text
        assert "绝对值" in text

    # 方法作用：验证多指标 × 分组的裁剪披露轮流的取法。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_multi_metric_group_trim_discloses_round_robin(self) -> None:
        """两个指标各 6 条共 12 条只留 8 条，用户要知道这是按指标轮流取的。"""
        # Arrange
        from src.reporting.charts import _MAX_SERIES, build_group_options

        rows = [
            {
                "year_month": "2026-01",
                "region": f"R{index}",
                "net_sales": index + 1,
                "order_count": index + 2,
            }
            for index in range(6)
        ]
        spec = ChartSpec(
            chart_type="bar",
            x_field="year_month",
            group_field="region",
            series_fields=["net_sales", "order_count"],
        )
        warnings: list[str] = []

        # Act
        _group, default_series, _options = build_group_options(spec=spec, rows=rows, warnings=warnings)

        # Assert
        assert len(default_series) == _MAX_SERIES
        text = " ".join(warnings)
        assert "12" in text and str(_MAX_SERIES) in text and "4" in text
        assert "轮流" in text

    # 方法作用：验证候选切换视图被裁剪时同样披露，且多个候选聚合成一条。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_candidate_views_share_one_aggregated_warning(self) -> None:
        """逐视图各写一条会把 section_warnings[:20] 挤满，真正的限制反而被顶掉。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {
                "year_month": "2026-01",
                "region": f"R{index:02d}",
                "channel": f"C{index % 10:02d}",
                "store": f"S{index % 9:02d}",
                "amount": index + 1,
            }
            for index in range(12)
        ]
        spec = ChartSpec(
            chart_type="bar",
            x_field="year_month",
            group_field="region",
            series_fields=["amount"],
            switchable_fields=["channel", "store"],
        )
        warnings: list[str] = []

        # Act
        _group, default_series, options = build_group_options(spec=spec, rows=rows, warnings=warnings)

        # Assert：三个视图都被裁到 8 条，但只出一条说明，且三个视图都在里面
        assert len(default_series) == 8
        assert all(len(item) == 8 for item in options.values())
        trimmed = [item for item in warnings if "系列数超出单图可读上限" in item]
        assert len(trimmed) == 1
        text = trimmed[0]
        assert "12→8" in text and "10→8" in text and "9→8" in text
        assert "绝对值" in text
        assert len(warnings) == 1

    # 方法作用：验证裁剪披露能一路进入页面 quality 块，而不是停在内部参数上。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_trim_disclosure_reaches_quality_block(self) -> None:
        """只停在 warnings 形参或日志里的披露，用户一个字也看不到。"""
        # Arrange
        from src.reporting.document import build_report_document

        unit = UnitResult(
            unit_id="u1",
            columns=["year_month", "region_code", "amount"],
            rows=[
                {"year_month": "2026-01", "region_code": f"R{index:02d}", "amount": index + 1}
                for index in range(12)
            ],
        )
        plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="u1", question="按区域统计金额")])
        chart_plan = ReportChartPlan(charts=[ChartSpec(
            chart_type="bar",
            x_field="year_month",
            group_field="region_code",
            series_fields=["amount"],
        )])

        # Act
        artifact = build_report_document(plan, [unit], chart_plans={"u1": chart_plan})

        # Assert
        limitations = [
            item
            for section in artifact.document.sections
            for block in section.blocks
            if block.type == "quality"
            for item in block.content.get("limitations", [])
        ]
        assert any("系列数超出单图可读上限" in item for item in limitations)


class TestChartNoSecondOrderComparison:
    """覆盖本轮回归：指标本身已是同比/环比时，不得再生成自动比较视图。"""

    # 方法作用：验证列名 token 判定按边界切分，不误伤同前缀词。
    # Args: self - 测试实例；column - 列名；expected - 期望判定。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("column", "expected"),
        [
            ("net_sales_yoy_pct", True),
            ("net_sales_mom_pct", True),
            ("yoy", True),
            ("mom_pct", True),
            ("净销售额同比", True),
            ("净销售额环比", True),
            ("net_sales_amount", False),
            ("momentum_score", False),
            ("momentum", False),
            ("gross_margin_pct", False),
            ("", False),
        ],
    )
    def test_comparison_metric_detection_uses_token_boundary(self, column: str, expected: bool) -> None:
        """`momentum_score` 不是 mom：按整体 token 相等判定，不做子串包含。"""
        # Arrange + Act
        from src.reporting.charts import _is_comparison_metric

        # Assert
        assert _is_comparison_metric(column) is expected

    # 方法作用：验证同比/环比指标图不再附加自动比较视图。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_comparison_metric_gets_no_auto_comparison_views(self) -> None:
        """真实 chart_sales_foundation_4 的 series_fields 已是同比/环比，再算一次是二阶比较。"""
        # Arrange
        from src.reporting.charts import build_group_options

        for column, base_value in (("net_sales_yoy_pct", 63.492), ("net_sales_mom_pct", 12.5)):
            rows = [
                {
                    "year_month": f"{2016 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}",
                    column: base_value + month,
                }
                for month in range(1, 14)
            ]
            spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=[column])

            # Act
            _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

            # Assert：13 个月本来够出同比，但指标已是比较值，两个都不得生成
            assert "__mom__" not in options, f"{column} 不得生成环比"
            assert "__yoy__" not in options, f"{column} 不得生成同比"

    # 方法作用：验证比较语义只体现在 labels 映射里时，生产路径同样能拦住二阶比较。
    # Args: self - 测试实例；label - 列的中文展示标签。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("label", ["净销售额同比", "净销售额环比", "销售额同比(%)"])
    def test_label_only_comparison_metric_blocks_both_views(self, label: str) -> None:
        """真实 ChartSpec 的字段名多是数据库英文列名，比较语义落在 labels 里。

        `growth_rate` 本身不含 mom/yoy token，旧逻辑只看字段名，于是给「同比」这一列
        又叠了同比与环比——正是要拦的「同比的环比」。
        """
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {
                "year_month": f"{2016 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}",
                "growth_rate": 50 + month,
            }
            for month in range(1, 14)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["growth_rate"])

        # Act
        _group, _series, options = build_group_options(
            spec=spec,
            rows=rows,
            labels={"growth_rate": label},
            warnings=[],
        )

        # Assert：13 个月本够出同比，但展示标签已是对比读数，两个都不得生成
        assert "__mom__" not in options, f"{label} 不得生成环比"
        assert "__yoy__" not in options, f"{label} 不得生成同比"

    # 方法作用：验证非比较中文标签不被误伤，合法比较视图仍然保留。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_comparison_label_keeps_comparison_views(self) -> None:
        """`增长动量评分` 不含「同比/环比」，按标签判定不得把它当成比较指标。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {
                "year_month": f"{2016 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}",
                "growth_rate": 50 + month,
            }
            for month in range(1, 14)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["growth_rate"])

        # Act
        _group, _series, options = build_group_options(
            spec=spec,
            rows=rows,
            labels={"growth_rate": "增长动量评分"},
            warnings=[],
        )

        # Assert
        assert "__mom__" in options
        assert "__yoy__" in options

    # 方法作用：验证声明里含比较指标时整张图都不附加自动比较视图。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mixed_declaration_with_comparison_metric_blocks_both_views(self) -> None:
        """声明里混了一个比较指标：逐指标元数据不可靠，整图不做二阶比较。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {
                "year_month": f"{2016 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}",
                "net_sales_amount": month * 10,
                "net_sales_yoy_pct": 60 + month,
            }
            for month in range(1, 14)
        ]
        spec = ChartSpec(
            chart_type="line",
            x_field="year_month",
            series_fields=["net_sales_amount", "net_sales_yoy_pct"],
        )

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        assert "__mom__" not in options
        assert "__yoy__" not in options

    # 方法作用：验证普通月度原始指标仍然保留合法的环比与同比。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_plain_monthly_metric_keeps_comparison_views(self) -> None:
        """修复不得波及普通金额指标：`net_sales_amount` 的环比/同比必须还在。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {
                "year_month": f"{2016 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}",
                "net_sales_amount": month * 10,
            }
            for month in range(1, 14)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["net_sales_amount"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        assert "__mom__" in options
        assert "__yoy__" in options

    # 方法作用：验证形似 mom 但非比较指标的列不触发守卫。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_momentum_score_still_gets_comparison_views(self) -> None:
        """`momentum_score` 只是以 mom 开头，不是环比指标，月度比较视图照常生成。"""
        # Arrange
        from src.reporting.charts import build_group_options

        rows = [
            {
                "year_month": f"{2016 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}",
                "momentum_score": 50 + month,
            }
            for month in range(1, 14)
        ]
        spec = ChartSpec(chart_type="line", x_field="year_month", series_fields=["momentum_score"])

        # Act
        _group, _series, options = build_group_options(spec=spec, rows=rows, warnings=[])

        # Assert
        assert "__mom__" in options
        assert "__yoy__" in options
