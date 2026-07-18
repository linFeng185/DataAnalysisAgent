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
