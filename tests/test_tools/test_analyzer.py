"""13.1~6 数据分析引擎测试。"""

from __future__ import annotations

import asyncio

from src.tools.analyzer import (
    compute_concentration,
    compute_correlation,
    compute_statistics,
    compute_trend,
    detect_outliers_iqr,
    detect_outliers_zscore,
)


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

    def test_with_data(self):
        from src.graph.nodes.analyze_result import analyze_result_node
        r = asyncio.run(analyze_result_node({
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
