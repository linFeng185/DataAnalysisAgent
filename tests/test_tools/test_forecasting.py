"""Phase D 预测与回测测试。"""

from __future__ import annotations

import pytest


class TestForecasting:
    """覆盖 baseline、rolling backtest、预测区间和输入校验。"""

    def test_forecast_series_uses_backtest_and_returns_interval(self):
        """趋势序列应返回选择模型、回测指标和上下界。"""
        # Arrange
        from src.tools.forecasting import forecast_series

        values = [10, 12, 14, 16, 18, 20, 22, 24, 26, 28]

        # Act
        result = forecast_series(values, horizon=3)

        # Assert
        assert result.model in {"naive", "linear"}
        assert len(result.predictions) == 3
        assert len(result.intervals) == 3
        assert all(item["lower"] <= item["value"] <= item["upper"] for item in result.intervals)
        assert "naive" in result.backtest
        assert result.model_card["leakage_check"] == "passed"

    def test_backtest_rejects_short_or_invalid_horizon(self):
        """样本不足和非法预测步长必须拒绝，不能伪造置信区间。"""
        # Arrange
        from src.tools.forecasting import ForecastingError, rolling_backtest

        # Act / Assert
        with pytest.raises(ForecastingError, match="样本不足"):
            rolling_backtest([1, 2, 3], horizon=2, min_train=3)
        with pytest.raises(ForecastingError, match="horizon"):
            rolling_backtest([1, 2, 3, 4], horizon=0)

    def test_forecast_rows_validates_time_order_and_numeric_values(self):
        """按行预测必须检查时间列递增和数值列完整性。"""
        # Arrange
        from src.tools.forecasting import ForecastingError, forecast_rows

        rows = [
            {"date": "2026-01-01", "value": 1},
            {"date": "2026-01-02", "value": 2},
            {"date": "2026-01-03", "value": 3},
            {"date": "2026-01-04", "value": 4},
        ]

        # Act
        result = forecast_rows(rows, time_col="date", value_col="value", horizon=2)

        # Assert
        assert result.time_col == "date"
        assert result.predictions
        with pytest.raises(ForecastingError, match="递增"):
            forecast_rows(list(reversed(rows)), time_col="date", value_col="value", horizon=2)
