"""覆盖 28.4、28.5：语言和多维图表的数据完整性回归。"""

from copy import deepcopy

import pytest


class TestChartPresentation:
    """覆盖 28.5：所有图表共用语义构型，不覆盖或补造数据。"""

    # 验证地区编码可作分类维度，但不能误当数值指标。
    # Args: self - 实例；kind - 图表类型；numeric_code - 编码是否采用整数。
    # Returns: 无返回值。
    @pytest.mark.parametrize("kind", ["line", "bar", "pie", "scatter", "heatmap"])
    @pytest.mark.parametrize("numeric_code", [False, True])
    def test_region_codes_remain_grouping_dimensions(self, kind: str, numeric_code: bool) -> None:
        """复现真实查询的 region_code：五类图表都应保留地区分组。"""
        from src.tools.chart_presentation import adapt_plan, build_option, numeric_columns

        # Arrange
        rows = [
            {
                "order_date": day,
                "region_code": region if numeric_code else f"R{region}",
                "net_sales_amount": 10 * index + region,
                "order_count": index + region,
            }
            for index, day in enumerate(["2026-01-01", "2026-01-02"], start=1)
            for region in (1, 2)
        ]
        original = deepcopy(rows)
        plan = {
            "x": "order_date",
            "y": "net_sales_amount",
            "series": ["region_code"],
            "metrics": ["net_sales_amount"],
            "aggregation": "none",
        }

        # Act
        effective = adapt_plan(rows, kind, plan)
        option = build_option(rows, kind, effective, {"region_code": "地区"})

        # Assert
        assert "region_code" not in numeric_columns(rows)
        assert option["series"]
        assert rows == original
        if kind in {"line", "bar"}:
            assert option["xAxis"]["data"] == ["2026-01-01", "2026-01-02"]
            assert len(option["series"]) == 2
            assert sum(value for series in option["series"] for value in series["data"]) == 66

    # 验证初始图表节点接受真实模型返回的地区编码方案。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_graph_accepts_real_region_code_plan(self) -> None:
        """真实链路保存的方案不能在图表节点被拒绝。"""
        from src.graph.nodes.generate_chart import generate_chart_node

        # Arrange
        state = {
            "user_query": "每天各地区销售",
            "display_language": "zh-CN",
            "query_result_sample": [
                {"order_date": "2026-01-01", "region_code": "R1", "net_sales_amount": 10},
                {"order_date": "2026-01-01", "region_code": "R2", "net_sales_amount": 20},
            ],
            "analysis_result": {
                "recommended_chart_type": "line",
                "chart_plan": {
                    "x": "order_date",
                    "y": "net_sales_amount",
                    "series": ["region_code"],
                    "metrics": ["net_sales_amount"],
                    "aggregation": "none",
                },
            },
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        assert result["chart_config"]["status"] == "success"
        assert len(result["chart_config"]["option"]["series"]) == 2

    # 验证第三维度保留为系列，图表切换不丢失查询值。
    # Args: self - 测试实例；kind - 图表类型。
    # Returns: 无返回值，失败由 pytest 报告。
    @pytest.mark.parametrize("kind", ["bar", "line"])
    def test_three_dimensions_preserve_all_values(self, kind: str) -> None:
        """日期作横轴，地区与产品组合系列，八条数据全部保留。"""
        from src.tools.chart_presentation import build_option

        # Arrange
        rows = [
            {"date": day, "region": region, "product": product, "sales": index + 1}
            for index, (day, region, product) in enumerate(
                (day, region, product)
                for day in ["2026-10-01", "2026-10-02"]
                for region in ["华东", "华南"]
                for product in ["A", "B"]
            )
        ]
        original = deepcopy(rows)
        # Act
        option = build_option(rows, kind)
        # Assert
        assert option["xAxis"]["data"] == ["2026-10-01", "2026-10-02"]
        assert len(option["series"]) == 4
        assert sum(sum(series["data"]) for series in option["series"]) == 36
        assert rows == original

    # 验证缺失坐标和值保持 null，同时折线跨缺失连接。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_missing_values_are_null_and_lines_connect(self) -> None:
        """没有数据不等于零，connectNulls 只影响画线。"""
        from src.tools.chart_presentation import build_option

        # Arrange
        rows = [
            {"date": "2026-10-01", "region": "A", "sales": 0},
            {"date": "2026-10-01", "region": "B", "sales": 2},
            {"date": "2026-10-02", "region": "A", "sales": None},
            {"date": "2026-10-03", "region": "A", "sales": 3},
            {"date": "2026-10-03", "region": "B", "sales": 4},
        ]
        # Act
        option = build_option(rows, "line")
        # Assert
        assert option["series"][0]["data"] == [0.0, None, 3.0]
        assert option["series"][1]["data"] == [2.0, None, 4.0]
        assert all(series["connectNulls"] for series in option["series"])

    # 验证相同坐标没有其他维度时不会静默覆盖。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_duplicate_coordinates_require_explicit_aggregation(self) -> None:
        """无聚合口径应拒绝，明确求和后才允许合并。"""
        from src.tools.chart_presentation import build_option

        # Arrange
        rows = [{"region": "A", "sales": 2}, {"region": "A", "sales": 3}]
        # Act / Assert
        with pytest.raises(ValueError, match="重复"):
            build_option(rows, "bar")
        option = build_option(rows, "bar", {"aggregation": "sum"})
        assert option["series"][0]["data"] == [5.0]

    # 验证散点使用数值轴、热力图使用两个真实维度。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_scatter_and_heatmap_use_correct_roles(self) -> None:
        """不同图表不能复用分类柱状图的数据编码。"""
        from src.tools.chart_presentation import build_option

        # Arrange
        rows = [
            {"date": "2026-10-01", "region": "A", "sales": 2, "profit": 1},
            {"date": "2026-10-02", "region": "B", "sales": 3, "profit": 2},
        ]
        # Act
        scatter = build_option(rows, "scatter", {"x": "sales", "y": "profit"})
        heatmap = build_option(rows, "heatmap", {"metrics": ["sales"]})
        # Assert
        assert scatter["xAxis"]["type"] == "value"
        assert scatter["yAxis"]["type"] == "value"
        assert sum(len(series["data"]) for series in scatter["series"]) == 2
        assert heatmap["series"][0]["type"] == "heatmap"
        assert heatmap["xAxis"]["data"] == ["2026-10-01", "2026-10-02"]

    # 验证非法模型字段和负数饼图不会形成误导图表。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_invalid_plan_and_negative_pie_are_rejected(self) -> None:
        """后端构型必须校验模型角色与图表适用性。"""
        from src.tools.chart_presentation import build_option

        # Arrange
        rows = [{"region": "A", "sales": -2}, {"region": "B", "sales": 3}]
        # Act / Assert
        with pytest.raises(ValueError):
            build_option(rows, "bar", {"x": "invented"})
        with pytest.raises(ValueError, match="负"):
            build_option(rows, "pie")

    # 验证每个数值指标都有系列，而不是只取第一列。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_multiple_metrics_are_preserved(self) -> None:
        """多指标默认分面，防止不同单位混用同一轴。"""
        from src.tools.chart_presentation import build_option

        # Arrange
        rows = [{"region": "A", "sales": 2, "orders": 1}, {"region": "B", "sales": 3, "orders": 2}]
        # Act
        option = build_option(rows, "bar")
        # Assert
        assert len(option["series"]) == 2
        assert len(option["yAxis"]) == 2


class TestPresentationLanguage:
    """覆盖 28.4：语言按问题上下文选择，标签不修改原数据。"""

    # 复现跨源合并没有模型标签时的实际字段及占位标签回退。
    # Args: self - 实例；descriptions - 缺失、原键名或错误语言的标签。
    # Returns: 无返回值。
    @pytest.mark.parametrize("descriptions", [None, {}, {"region_code": "region_code", "sales_amount": "Sales amount"}])
    def test_cross_source_aliases_have_reliable_chinese_fallback(self, descriptions: dict | None) -> None:
        """已知销售额和地区编码可中文兜底，未知业务字段和地区原值不编造。"""
        from src.tools.chart_presentation import column_labels

        # Arrange
        rows = [{"order_date": "2026-01-01", "region_code": "CN-BJ", "sales_amount": 12,
                 "net_sales_amount": 10, "custom_metric": 2, "_datasource": "mysql"}]
        original = deepcopy(rows)
        # Act
        labels = column_labels(rows, descriptions, "zh-CN")
        # Assert
        assert labels["region_code"] == "地区编码"
        assert labels["sales_amount"] == "销售金额"
        assert labels["net_sales_amount"] == "净销售金额"
        assert labels["custom_metric"] == "custom_metric"
        assert rows == original

    # 验证可靠兜底不覆盖用户语言、业务中文名称及特殊指标缩写。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_alias_fallback_keeps_contextual_labels_and_language(self) -> None:
        """中文业务名称优先；英文输出不被中文映射覆盖，未知 GMV 保留。"""
        from src.tools.chart_presentation import column_labels

        # Arrange
        rows = [{"region_code": "CN-BJ", "sales_amount": 10, "custom_metric": 3}]
        # Act
        chinese = column_labels(rows, {"region_code": "销售区域", "sales_amount": "含税收入", "custom_metric": "GMV"}, "zh-CN")
        english = column_labels(rows, {"sales_amount": "Sales amount"}, "en-US")
        # Assert
        assert chinese == {"region_code": "销售区域", "sales_amount": "含税收入", "custom_metric": "GMV"}
        assert english["sales_amount"] == "Sales amount"
        assert english["region_code"] == "Region code"

    # 验证明确语言指令优先于问题语言和界面偏好。
    # Args: self - 测试实例；query - 用户问题；expected - 期望语言。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("请用英语展示销售额", "en-US"),
            ("Show sales in Chinese", "zh-CN"),
            ("各地区销售额", "zh-CN"),
            ("Compare regional sales", "en-US"),
            ("请用日语展示", "ja-JP"),
        ],
    )
    def test_language_follows_query(self, query: str, expected: str) -> None:
        """中文界面不能覆盖用户要求的英文输出。"""
        from src.tools.chart_presentation import resolve_language

        # Arrange / Act
        language = resolve_language(query, fallback="zh-CN")
        # Assert
        assert language == expected

    # 验证标签只来自真实结果列，未知键和过长值不会扩散。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_column_labels_are_localized_and_allowlisted(self) -> None:
        """标签单独映射，不能改写查询数据的字段和值。"""
        from src.tools.chart_presentation import column_labels

        # Arrange
        rows = [{"order_date": "2026-10-01", "total_sales": 2}]
        # Act
        labels = column_labels(rows, {"total_sales": "销售额", "unknown": "秘密"}, "zh-CN")
        # Assert
        assert labels == {"order_date": "订单日期", "total_sales": "销售额"}
        assert rows[0]["total_sales"] == 2


class TestChartSwitchContract:
    """覆盖 28.5：切换图表保留适用角色，不沿用不兼容的坐标轴角色。"""

    # 验证轴类切换到散点和热力图时选择兼容的真实字段。
    # Args: self - 实例；kind - 新类型。
    # Returns: 无返回值。
    @pytest.mark.parametrize("kind", ["scatter", "heatmap"])
    async def test_switch_adapts_roles_without_losing_data(self, kind: str) -> None:
        """日期横轴的柱状图方案不能直接成为散点图数值横轴。"""
        from src.api.routes.visualization import adjust_chart
        from src.api.schemas import ChartAdjustRequest

        # Arrange
        request = ChartAdjustRequest(
            rows=[
                {"date": "2026-10-01", "region": "A", "sales": 2, "profit": 1},
                {"date": "2026-10-02", "region": "B", "sales": 3, "profit": 2},
            ],
            instruction=kind,
            chart_plan={"x": "date", "y": "sales", "series": ["region"], "metrics": ["sales"]},
        )
        # Act
        result = await adjust_chart(request)
        # Assert
        assert result["type"] == kind
        assert result["option"]["series"]
        if kind == "scatter":
            assert result["option"]["xAxis"]["type"] == "value"
            assert sum(len(series["data"]) for series in result["option"]["series"]) == 2
        else:
            assert result["option"]["xAxis"]["data"] == ["2026-10-01", "2026-10-02"]

    # 验证明确聚合只合并已选定的坐标，不破坏原始数据。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_switch_preserves_explicit_aggregation(self) -> None:
        """初始合法 sum 方案切换时不能被无口径的工具预校验拒绝。"""
        from src.api.routes.visualization import adjust_chart
        from src.api.schemas import ChartAdjustRequest

        # Arrange
        request = ChartAdjustRequest(
            rows=[{"region": "A", "sales": 2}, {"region": "A", "sales": 3}],
            instruction="line",
            chart_plan={"aggregation": "sum"},
        )
        # Act
        result = await adjust_chart(request)
        # Assert
        assert result["option"]["series"][0]["data"] == [5.0]
