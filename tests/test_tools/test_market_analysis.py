"""Phase D 行情指标与数据提供者契约测试。"""

from __future__ import annotations

import pytest


class TestMarketAnalysis:
    """覆盖收益、回撤、波动率和 provider 元数据。"""

    def test_compute_market_metrics_contains_asof_and_drawdown(self):
        """价格序列应输出收益、波动、最大回撤和数据时点。"""
        # Arrange
        from src.tools.market_analysis import MarketPoint, compute_market_metrics

        points = [
            MarketPoint(symbol="AAA", timestamp="2026-01-01T00:00:00+00:00", close=100),
            MarketPoint(symbol="AAA", timestamp="2026-01-02T00:00:00+00:00", close=110),
            MarketPoint(symbol="AAA", timestamp="2026-01-03T00:00:00+00:00", close=90),
            MarketPoint(symbol="AAA", timestamp="2026-01-04T00:00:00+00:00", close=95),
        ]

        # Act
        result = compute_market_metrics(points, provider="test", adjustment="split_adjusted")

        # Assert
        assert result.symbol == "AAA"
        assert result.provider == "test"
        assert result.as_of == "2026-01-04T00:00:00+00:00"
        assert result.max_drawdown < 0
        assert result.adjustment == "split_adjusted"

    def test_market_metrics_rejects_mixed_symbols_or_nonpositive_price(self):
        """混合证券和非正价格必须拒绝，避免错误联想。"""
        # Arrange
        from src.tools.market_analysis import MarketAnalysisError, MarketPoint, compute_market_metrics

        # Act / Assert
        with pytest.raises(MarketAnalysisError, match="同一 symbol"):
            compute_market_metrics([
                MarketPoint("AAA", "2026-01-01", 1), MarketPoint("BBB", "2026-01-02", 2),
            ])
        with pytest.raises(MarketAnalysisError, match="正数"):
            compute_market_metrics([MarketPoint("AAA", "2026-01-01", 0)])
