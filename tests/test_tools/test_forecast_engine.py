"""可扩展预测模型引擎测试。"""

from __future__ import annotations

import pytest


class ConstantModel:
    """测试用常数预测模型。"""

    name = "constant"

    # 方法作用：记录训练序列均值。
    # Args: self - 模型；values - 训练值。
    # Returns: 模型状态。
    def fit(self, values):
        return {"value": sum(values) / len(values)}

    # 方法作用：输出指定步长的常数预测。
    # Args: self - 模型；state - fit 状态；horizon - 预测步长。
    # Returns: 预测值列表。
    def predict(self, state, horizon):
        return [state["value"]] * horizon


class TestForecastEngine:
    """覆盖模型注册、显式选择和统一结果契约。"""

    def test_register_model_and_forecast_with_explicit_model(self):
        """外部模型应能注册后通过统一 ForecastRequest 调用。"""
        # Arrange
        from src.tools.forecast_engine import ForecastEngine, ForecastRequest

        engine = ForecastEngine()
        engine.register(ConstantModel())
        request = ForecastRequest(target_column="sales", time_column="date", horizon=2, model="constant")

        # Act
        result = engine.forecast(request, [10, 20, 30, 40])

        # Assert
        assert result.model == "constant"
        assert result.predictions == [25.0, 25.0]
        assert result.model_card["extensible"] is True

    def test_auto_selection_uses_registered_models(self):
        """不指定模型时应在注册模型中选择回测误差较低者。"""
        # Arrange
        from src.tools.forecast_engine import ForecastEngine, ForecastRequest

        engine = ForecastEngine()
        engine.register(ConstantModel())
        request = ForecastRequest(target_column="sales", time_column="date", horizon=1, model="auto")

        # Act
        result = engine.forecast(request, [10, 10, 10, 10, 10, 10])

        # Assert
        assert result.model == "constant"
        assert result.backtest["constant"]["mae"] == 0.0

    def test_empty_registry_uses_existing_deterministic_fallback(self):
        """未注册模型时应回退到现有确定性预测实现。"""
        # Arrange
        from src.tools.forecast_engine import ForecastEngine, ForecastRequest

        engine = ForecastEngine()
        request = ForecastRequest(target_column="sales", time_column="date", horizon=2)

        # Act
        result = engine.forecast(request, [10, 11, 12, 13, 14, 15])

        # Assert
        assert result.model in {"naive", "linear"}
        assert result.model_card["extensible"] is True

    def test_forecast_rejects_non_numeric_input(self):
        """预测序列包含非数值时应转换为 ForecastingError。"""
        # Arrange
        from src.tools.forecast_engine import ForecastEngine, ForecastRequest
        from src.tools.forecasting import ForecastingError

        engine = ForecastEngine()
        request = ForecastRequest(target_column="sales", time_column="date", horizon=1)

        # Act / Assert
        with pytest.raises(ForecastingError, match="非数值"):
            engine.forecast(request, [1, 2, "bad", 4])
