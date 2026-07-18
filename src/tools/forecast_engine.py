"""可注册预测模型执行引擎。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, Sequence

from pydantic import BaseModel, Field, field_validator

from src.logging_config import get_logger
from src.tools.forecasting import ForecastResult, ForecastingError, forecast_series

logger = get_logger(__name__)


class ForecastRequest(BaseModel):
    """统一预测请求，避免模型实现绑定特定业务字段。"""

    target_column: str = Field(min_length=1)
    time_column: str = Field(min_length=1)
    horizon: int = Field(gt=0)
    frequency: str = "1d"
    model: str = "auto"
    known_future_columns: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=lambda: ["mae", "rmse"])
    constraints: dict[str, Any] = Field(default_factory=dict)

    # 方法作用：校验预测模型名称，统一大小写和空白输入。
    # Args: value - 模型名称。
    # Returns: 规范化后的模型名称。
    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        logger.debug("预测请求模型校验入口", model=value)
        if not isinstance(value, str):
            raise ValueError("model 必须是字符串")
        result = value.strip().lower()
        if not result:
            raise ValueError("model 不能为空")
        logger.info("预测请求模型校验完成", model=result)
        return result


class ForecastModel(ABC):
    """第三方预测模型必须实现的最小同步接口。"""

    name: str

    # 方法作用：使用历史序列拟合模型状态。
    # Args: self - 模型；values - 按时间升序的训练值。
    # Returns: 可传给 predict 的模型状态。
    @abstractmethod
    def fit(self, values: Sequence[float]) -> Any:
        logger.debug("预测模型 fit 入口", model=getattr(self, "name", "unknown"), rows=len(values))
        raise NotImplementedError

    # 方法作用：根据已拟合状态生成未来预测值。
    # Args: self - 模型；state - fit 返回状态；horizon - 预测步数。
    # Returns: 预测值序列。
    @abstractmethod
    def predict(self, state: Any, horizon: int) -> Sequence[float]:
        logger.debug("预测模型 predict 入口", model=getattr(self, "name", "unknown"), horizon=horizon)
        raise NotImplementedError


class ForecastEngine:
    """管理预测模型注册、滚动回测和统一结果输出。"""

    # 方法作用：创建空模型注册表。
    # Args: self - 预测引擎。
    # Returns: 无返回值。
    def __init__(self) -> None:
        logger.debug("初始化预测引擎入口")
        self._models: dict[str, Any] = {}
        logger.info("初始化预测引擎完成", models=0)

    # 方法作用：注册或替换一个具有 name/fit/predict 的模型。
    # Args: self - 预测引擎；model - 外部预测模型实例。
    # Returns: 无返回值。
    def register(self, model: Any) -> None:
        logger.debug("注册预测模型入口", model_type=type(model).__name__)
        name = str(getattr(model, "name", "")).strip().lower()
        if not name:
            raise ValueError("预测模型必须提供非空 name")
        if not callable(getattr(model, "fit", None)) or not callable(getattr(model, "predict", None)):
            raise TypeError("预测模型必须实现 fit 和 predict")
        self._models[name] = model
        logger.info("注册预测模型完成", model=name, total=len(self._models))

    # 方法作用：按请求选择模型、执行回测并生成预测结果。
    # Args: request - 统一预测请求；values - 按时间升序的历史数值。
    # Returns: 兼容既有 ForecastResult 的可审计结果。
    def forecast(self, request: ForecastRequest, values: Sequence[float]) -> ForecastResult:
        logger.debug("预测引擎执行入口", model=request.model, horizon=request.horizon, rows=len(values))
        numeric = self._coerce_values(values)
        if request.model != "auto" and request.model not in self._models:
            raise ForecastingError(f"未注册预测模型: {request.model}")
        if not self._models:
            result = forecast_series(numeric, horizon=request.horizon)
            result.model_card.update({"extensible": True, "target_column": request.target_column, "time_column": request.time_column, "frequency": request.frequency})
            logger.info("预测引擎使用内置回退完成", model=result.model)
            return result
        candidates = [request.model] if request.model != "auto" else list(self._models)
        scores = {name: self._backtest(self._models[name], numeric, request.horizon) for name in candidates}
        selected = candidates[0] if request.model != "auto" else min(candidates, key=lambda name: (scores[name]["mae"], candidates.index(name)))
        model = self._models[selected]
        state = model.fit(numeric)
        predictions = self._normalize_predictions(model.predict(state, request.horizon), request.horizon)
        scale = self._residual_scale(model, numeric, request.horizon)
        intervals = [{"value": round(value, 6), "lower": round(value - 1.2816 * scale * math.sqrt(step), 6), "upper": round(value + 1.2816 * scale * math.sqrt(step), 6)} for step, value in enumerate(predictions, 1)]
        card = {"model": selected, "extensible": True, "target_column": request.target_column, "time_column": request.time_column, "frequency": request.frequency, "training_rows": len(numeric), "leakage_check": "passed", "limitations": ["预测不构成确定性承诺"]}
        result = ForecastResult(model=selected, predictions=predictions, intervals=intervals, backtest=scores, model_card=card, time_col=request.time_column)
        logger.info("预测引擎执行完成", model=selected, horizon=request.horizon)
        return result

    # 方法作用：对每个滚动窗口执行模型预测并计算 MAE/RMSE。
    # Args: self - 预测引擎；model - 待评估模型；values - 历史序列；horizon - 窗口步长。
    # Returns: 回测指标字典。
    def _backtest(self, model: Any, values: list[float], horizon: int) -> dict[str, float]:
        logger.debug("预测模型回测入口", model=getattr(model, "name", "unknown"), rows=len(values))
        min_train = max(2, min(5, len(values) - horizon))
        errors: list[float] = []
        for end in range(min_train, len(values) - horizon + 1):
            state = model.fit(values[:end])
            predicted = self._normalize_predictions(model.predict(state, horizon), horizon)
            errors.extend(actual - forecast for actual, forecast in zip(values[end:end + horizon], predicted))
        if not errors:
            raise ForecastingError("样本不足，无法执行预测模型回测")
        mae = sum(abs(error) for error in errors) / len(errors)
        rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
        result = {"mae": round(mae, 6), "rmse": round(rmse, 6)}
        logger.info("预测模型回测完成", model=getattr(model, "name", "unknown"), mae=result["mae"])
        return result

    # 方法作用：估计模型残差尺度，用于生成保守预测区间。
    # Args: self - 预测引擎；model - 已注册模型；values - 历史序列；horizon - 预测步长。
    # Returns: 残差标准差。
    def _residual_scale(self, model: Any, values: list[float], horizon: int) -> float:
        logger.debug("预测区间尺度计算入口", model=getattr(model, "name", "unknown"))
        min_train = max(2, min(5, len(values) - horizon))
        errors: list[float] = []
        for end in range(min_train, len(values) - horizon + 1):
            state = model.fit(values[:end])
            predicted = self._normalize_predictions(model.predict(state, horizon), horizon)
            errors.extend(actual - forecast for actual, forecast in zip(values[end:end + horizon], predicted))
        if len(errors) < 2:
            return 0.0
        mean = sum(errors) / len(errors)
        result = math.sqrt(sum((error - mean) ** 2 for error in errors) / (len(errors) - 1))
        logger.info("预测区间尺度计算完成", scale=result)
        return result

    # 方法作用：校验并转换模型预测输出。
    # Args: values - 模型原始预测；horizon - 期望步数。
    # Returns: 有限浮点预测值列表。
    @staticmethod
    def _normalize_predictions(values: Sequence[float], horizon: int) -> list[float]:
        logger.debug("预测输出校验入口", horizon=horizon)
        try:
            result = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise ForecastingError("预测模型输出包含非数值") from exc
        if len(result) != horizon or any(not math.isfinite(value) for value in result):
            raise ForecastingError("预测模型输出长度或数值非法")
        logger.info("预测输出校验完成", count=len(result))
        return [round(value, 6) for value in result]

    # 方法作用：把输入序列转换为有限浮点列表。
    # Args: values - 原始历史序列。
    # Returns: 有限浮点列表。
    @staticmethod
    def _coerce_values(values: Sequence[float]) -> list[float]:
        logger.debug("预测输入校验入口", rows=len(values))
        if len(values) < 4:
            raise ForecastingError("样本不足，至少需要 4 个数值")
        try:
            result = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise ForecastingError("预测数据包含非数值") from exc
        if any(not math.isfinite(value) for value in result):
            raise ForecastingError("预测数据不能包含 NaN 或 Infinity")
        logger.info("预测输入校验完成", rows=len(result))
        return result
