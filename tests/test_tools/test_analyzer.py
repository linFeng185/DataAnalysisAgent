"""13.1~6 数据分析引擎测试。"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from src.tools.analyzer import (
    compute_concentration,
    compute_correlation,
    compute_statistics,
    compute_trend,
    detect_outliers_iqr,
    detect_outliers_zscore,
)

logger = logging.getLogger(__name__)


class TestStats:
    """13.1"""

    def test_basic(self):
        r = compute_statistics([{"a": 1}, {"a": 3}, {"a": 5}])
        assert r["columns"]["a"]["mean"] == 3.0
        assert r["columns"]["a"]["median"] == 3.0

    def test_empty(self):
        assert compute_statistics([])["row_count"] == 0

    def test_nulls(self):
        r = compute_statistics([{"a": 1}, {"a": None}, {"a": 3}])
        assert r["columns"]["a"]["null_count"] == 1

    def test_skips_strings(self):
        r = compute_statistics([{"a": 1, "b": "x"}])
        assert "b" not in r["numeric_columns"]

    # 验证数据库 Decimal 数值能进入统计计算并排除非数值字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言 Decimal 列识别与统计结果。
    def test_decimal_is_recognized_as_numeric(self):
        """Decimal 销售额应被识别为数值列并正确计算均值。"""
        logger.debug("test_decimal_is_recognized_as_numeric 入口")
        # Arrange
        rows = [
            {"category": "食品饮料", "sales": Decimal("10.25")},
            {"category": "图书文娱", "sales": Decimal("20.75")},
        ]

        # Act
        result = compute_statistics(rows)

        # Assert
        assert result["numeric_columns"] == ["sales"]
        assert result["columns"]["sales"]["mean"] == 15.5
        logger.info("test_decimal_is_recognized_as_numeric 完成", extra={"numeric_columns": result["numeric_columns"]})


class TestTrend:
    """13.2"""

    def test_up(self):
        r = compute_trend([{"t": "a", "v": 10}, {"t": "b", "v": 30}], "t", "v")
        assert r["trend"] == "up"

    def test_down(self):
        r = compute_trend([{"t": "a", "v": 100}, {"t": "b", "v": 50}], "t", "v")
        assert r["trend"] == "down"

    def test_empty(self):
        assert compute_trend([], "t", "v")["trend"] == "flat"

    # 方法作用：验证趋势分析可以直接处理数据库返回的 Decimal 数值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言趋势方向和环比结果正确。
    def test_decimal_values_are_supported(self) -> None:
        """Decimal 输入不得与浮点阈值混算而抛出 TypeError。"""
        # Arrange
        rows = [
            {"t": "2026-01", "v": Decimal("10")},
            {"t": "2026-02", "v": Decimal("20")},
        ]

        # Act
        result = compute_trend(rows, "t", "v")

        # Assert
        assert result["trend"] == "up"
        assert result["change_pct"] == Decimal("100.00")


class TestOutliers:
    """13.3-4"""

    def test_zscore_none(self):
        assert detect_outliers_zscore([1, 2, 3, 2, 1, 2, 3, 2]) == []

    def test_zscore_found(self):
        """阈值 2.0 捕获明显离群值。"""
        r = detect_outliers_zscore([1, 2, 2, 1, 2, 100], threshold=2.0)
        assert len(r) >= 1

    def test_zscore_none_default_threshold(self):
        """默认阈值 3.0 下单离群值可能不触发(小样本 z 有上限)。"""
        r = detect_outliers_zscore([1, 2, 2, 1, 2, 100])
        assert isinstance(r, list)

    def test_iqr_none(self):
        assert detect_outliers_iqr([1, 2, 3, 4, 5, 2, 3, 4]) == []

    def test_iqr_found(self):
        r = detect_outliers_iqr([1, 2, 3, 4, 5, 2, 3, 500])
        assert len(r) >= 1

    # 方法作用：验证 Z-Score 在统一数值域中处理 Decimal 输入。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言离群值可被识别。
    def test_zscore_decimal_values(self) -> None:
        """Decimal 序列计算均值和标准差时不得发生类型混算。"""
        # Arrange
        values = [Decimal("1"), Decimal("2"), Decimal("2"), Decimal("100")]

        # Act
        result = detect_outliers_zscore(values, threshold=1.0)

        # Assert
        assert result[-1]["index"] == 3
        assert result[-1]["value"] == 100.0

    # 方法作用：验证 IQR 在统一数值域中处理 Decimal 输入。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言上界离群值可被识别。
    def test_iqr_decimal_values(self) -> None:
        """Decimal 四分位数与浮点系数不得在同一表达式中混算。"""
        # Arrange
        values = [
            Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"),
            Decimal("5"), Decimal("2"), Decimal("3"), Decimal("500"),
        ]

        # Act
        result = detect_outliers_iqr(values)

        # Assert
        assert result == [{"index": 7, "value": 500.0, "bound": "upper"}]


class TestConcentration:
    """13.5"""

    def test_top3(self):
        assert compute_concentration([10, 5, 3, 1, 1], 3)["top_concentration"] == 90.0

    def test_empty(self):
        assert compute_concentration([])["top_concentration"] == 0


class TestCorrelation:
    """13.6"""

    def test_perfect_positive(self):
        assert compute_correlation([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == 1.0

    def test_negative(self):
        assert compute_correlation([1, 2, 3], [3, 2, 1]) == -1.0

    def test_too_few(self):
        assert compute_correlation([1, 2], [3, 4]) == 0

    # 方法作用：验证 Pearson 相关系数可以接收两列 Decimal 数值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言完全正相关结果为 1.0。
    def test_decimal_values_are_supported(self) -> None:
        """相关系数入口应统一 Decimal 数值域后再执行近似统计。"""
        # Arrange
        left = [Decimal("1"), Decimal("2"), Decimal("3")]
        right = [Decimal("2"), Decimal("4"), Decimal("6")]

        # Act
        result = compute_correlation(left, right)

        # Assert
        assert result == 1.0


class TestAnalyzeResultNode:
    """4.8 集成"""

    def test_with_data(self, monkeypatch):
        import src.graph.nodes.analyze_result as analyze_module
        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: False)
        r = asyncio.run(analyze_module.analyze_result_node({
            "query_result_sample": [
                {"category": "电子", "sales": 128000},
                {"category": "家居", "sales": 102000},
                {"category": "美妆", "sales": 98000},
            ],
            "intent": "aggregation",
        }))
        a = r["analysis_result"]
        assert len(a["insights"]) > 0
        assert a["recommended_chart_type"] in ("bar", "table", "line", "pie")
        assert "statistics" in a

    def test_empty(self):
        from src.graph.nodes.analyze_result import analyze_result_node
        r = asyncio.run(analyze_result_node({"query_result_sample": []}))
        assert "无数据" in r["analysis_result"]["summary"]

    # 方法作用：验证通用聚合处理器不会截断后续业务分析。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_order_query_without_llm_contains_business_analysis(self, monkeypatch):
        """固定工单结果仍需给出关联指标、影响、假设和条件化建议。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: False)
        rows = [
            {"f_date": "2025-12-29", "total_daily_demand_orders": 50},
            {"f_date": "2025-12-30", "total_daily_demand_orders": 50},
            {"f_date": "2025-12-31", "total_daily_demand_orders": 50},
        ]
        state = {
            "user_query": "分析一下所有的单日需求工单数",
            "intent": "query",
            "query_result_sample": rows,
            "query_result_full_count": 3,
            "generated_sql": "SELECT f_date, SUM(demand_orders) FROM daily_orders GROUP BY f_date",
        }

        # Act
        analysis = asyncio.run(analyze_module.analyze_result_node(state))["analysis_result"]

        # Assert
        assert "均为 50" in analysis["summary"]
        assert analysis["insights"] == [
            "3 条记录的最小值、最大值和均值均为 50，极差和标准差均为 0。"
        ]
        assert analysis["recommended_chart_type"] == "line"
        assert analysis["related_metrics"]
        assert any("交付工单" in item for item in analysis["related_metrics"])
        assert analysis["business_impacts"]
        assert all("待验证" in item for item in analysis["possible_causes"])
        assert any("若" in item for item in analysis["recommended_actions"])
        assert analysis["confidence"] == "medium"

    # 方法作用：验证处理器精确计算后会继续调用完整分析模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_processor_result_continues_to_llm_business_analysis(self, monkeypatch):
        """模型可用时不得只执行摘要润色，必须返回完整业务解读契约。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.graph.nodes.analyze_result as analyze_module

        llm_result = {
            "summary": "三天需求工单数持平",
            "insights": ["三天均为50"],
            "data_quality": [],
            "limitations": ["不能证明原因"],
            "confidence": "medium",
            "related_metrics": ["交付工单数"],
            "business_impacts": ["可能影响排产"],
            "possible_causes": ["待验证：批量下单"],
            "recommended_actions": ["联查交付工单"],
            "recommended_chart_type": "line",
            "follow_up_questions": ["是否按车间拆分"],
        }
        llm_analyze = AsyncMock(return_value=llm_result)
        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(analyze_module, "_llm_analyze", llm_analyze)
        state = {
            "user_query": "分析单日需求工单数",
            "intent": "query",
            "query_result_sample": [
                {"f_date": "2025-12-29", "total_daily_demand_orders": 50},
                {"f_date": "2025-12-30", "total_daily_demand_orders": 50},
            ],
            "query_result_full_count": 2,
            "generated_sql": "SELECT f_date, SUM(demand_orders) FROM daily_orders GROUP BY f_date",
        }

        # Act
        analysis = asyncio.run(analyze_module.analyze_result_node(state))["analysis_result"]

        # Assert
        llm_analyze.assert_awaited_once()
        assert "处理器摘要" in llm_analyze.await_args.args[3]
        assert analysis["related_metrics"] == ["交付工单数"]
        assert analysis["recommended_actions"] == ["联查交付工单"]
        assert analysis["processor_name"] == "aggregation"
        assert "statistics" in analysis

    # 验证 Decimal 聚合结果仍会推荐可视化图表。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言聚合分析推荐柱状图。
    def test_decimal_aggregation_recommends_bar_chart(self, monkeypatch):
        """分类销售额为 Decimal 时，聚合处理器应输出高置信度柱状图。"""
        logger.debug("test_decimal_aggregation_recommends_bar_chart 入口")
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: False)
        state = {
            "query_result_sample": [
                {"category_name": "食品饮料", "total_sales": Decimal("645000000.25")},
                {"category_name": "图书文娱", "total_sales": Decimal("612000000.75")},
            ],
            "intent": "query",
        }

        # Act
        result = asyncio.run(analyze_module.analyze_result_node(state))["analysis_result"]

        # Assert
        assert result["statistics"]["numeric_columns"] == ["total_sales"]
        assert result["recommended_chart_type"] == "bar"
        logger.info("test_decimal_aggregation_recommends_bar_chart 完成", extra={"chart_type": result["recommended_chart_type"]})
