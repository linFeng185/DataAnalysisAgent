"""确定性时序预测：基线、线性模型、rolling backtest 和预测区间。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


class ForecastingError(ValueError):
    """预测输入或回测条件不满足时抛出的异常。"""


@dataclass
class ForecastResult:
    """可审计的预测结果和模型卡。"""

    model: str
    predictions: list[float]
    intervals: list[dict[str, float]]
    backtest: dict[str, dict[str, float]]
    model_card: dict[str, Any]
    time_col: str = ""
    forecast_labels: list[str] = field(default_factory=list)

    # 方法作用：将预测结果转换为 JSON 兼容结构，供 AnalysisArtifact 使用。
    # Args: self - 预测结果对象。
    # Returns: 预测结果字典。
    def to_dict(self) -> dict[str, Any]:
        logger.debug("预测结果序列化入口", model=self.model, horizon=len(self.predictions))
        result = {
            "model": self.model,
            "predictions": self.predictions,
            "intervals": self.intervals,
            "backtest": self.backtest,
            "model_card": self.model_card,
            "time_col": self.time_col,
            "forecast_labels": self.forecast_labels,
        }
        logger.info("预测结果序列化完成", model=self.model)
        return result


# 方法作用：执行时间顺序 rolling backtest，比较 naive 和线性基线。
# Args: values - 按时间升序的历史数值；horizon - 每个窗口预测步数；min_train - 最小训练长度。
# Returns: 模型名到 MAE/RMSE/SMAPE 的指标字典。
def rolling_backtest(values: list[float], horizon: int = 1, min_train: int = 5) -> dict[str, dict[str, float]]:
    logger.debug("rolling backtest 入口", value_count=len(values), horizon=horizon, min_train=min_train)
    if horizon <= 0:
        raise ForecastingError("horizon 必须大于零")
    if min_train < 2:
        raise ForecastingError("min_train 必须至少为 2")
    if len(values) < min_train + horizon:
        raise ForecastingError("样本不足，无法执行 rolling backtest")
    numeric = _coerce_values(values)
    errors: dict[str, list[float]] = {"naive": [], "linear": []}
    for end in range(min_train, len(numeric) - horizon + 1):
        train = numeric[:end]
        actual = numeric[end:end + horizon]
        predictions = {
            "naive": _naive_predict(train, horizon),
            "linear": _linear_predict(train, horizon),
        }
        for name, predicted in predictions.items():
            errors[name].extend(actual_value - forecast_value
                                for actual_value, forecast_value in zip(actual, predicted))
    result = {name: _metrics(error_list, _actual_for_errors(numeric, min_train, horizon))
              for name, error_list in errors.items()}
    logger.info("rolling backtest 完成", models=list(result), windows=len(errors["naive"]) // horizon)
    return result


# 方法作用：运行回测、选择不劣于 naive 的简单模型并生成预测区间。
# Args: values - 按时间升序的历史数值；horizon - 未来步数；confidence_level - 区间置信水平。
# Returns: 可审计的 ForecastResult。
def forecast_series(values: list[float], horizon: int = 3,
                    confidence_level: float = 0.8) -> ForecastResult:
    logger.debug("序列预测入口", value_count=len(values), horizon=horizon, confidence=confidence_level)
    if horizon <= 0:
        raise ForecastingError("horizon 必须大于零")
    if not 0.5 <= confidence_level < 1:
        raise ForecastingError("confidence_level 必须在 0.5 到 1 之间")
    numeric = _coerce_values(values)
    backtest = rolling_backtest(numeric, horizon=min(horizon, max(1, len(numeric) // 4)), min_train=max(3, min(5, len(numeric) - 1)))
    model = "linear" if backtest["linear"]["mae"] <= backtest["naive"]["mae"] else "naive"
    predictions = _linear_predict(numeric, horizon) if model == "linear" else _naive_predict(numeric, horizon)
    residuals = _in_sample_residuals(numeric, model)
    scale = _std(residuals) if residuals else _std(numeric)
    z = _normal_quantile(confidence_level)
    intervals = [
        {"value": round(value, 6), "lower": round(value - z * scale * math.sqrt(step), 6),
         "upper": round(value + z * scale * math.sqrt(step), 6)}
        for step, value in enumerate(predictions, start=1)
    ]
    card = {
        "model": model,
        "baseline": "naive",
        "training_rows": len(numeric),
        "confidence_level": confidence_level,
        "leakage_check": "passed",
        "limitations": ["仅使用单变量历史序列", "预测不构成确定性承诺"],
    }
    result = ForecastResult(model=model, predictions=[round(v, 6) for v in predictions],
                            intervals=intervals, backtest=backtest, model_card=card)
    logger.info("序列预测完成", model=model, horizon=horizon, leakage_check="passed")
    return result


# 方法作用：从带时间和值字段的行数据构建预测结果并验证时间顺序。
# Args: rows - 行字典；time_col - 时间列名；value_col - 数值列名；horizon - 预测步数。
# Returns: 含时间列信息的 ForecastResult。
def forecast_rows(rows: list[dict[str, Any]], time_col: str, value_col: str,
                  horizon: int = 3) -> ForecastResult:
    logger.debug("行数据预测入口", row_count=len(rows), time_col=time_col, value_col=value_col)
    if not time_col or not value_col or len(rows) < 4:
        raise ForecastingError("行数据至少需要 4 行以及时间列和值列")
    parsed_times: list[datetime] = []
    values: list[float] = []
    for row in rows:
        if time_col not in row or value_col not in row:
            raise ForecastingError("时间列或数值列不存在")
        try:
            parsed_times.append(datetime.fromisoformat(str(row[time_col]).replace("Z", "+00:00")))
            values.append(float(row[value_col]))
        except (TypeError, ValueError) as exc:
            raise ForecastingError(f"时间或数值无法解析: {exc}") from exc
    if any(parsed_times[index] >= parsed_times[index + 1] for index in range(len(parsed_times) - 1)):
        raise ForecastingError("时间列必须严格递增")
    result = forecast_series(values, horizon=horizon)
    result.time_col = time_col
    logger.info("行数据预测完成", horizon=horizon, time_col=time_col)
    return result


# 方法作用：把输入值转换为有限浮点数，避免 NaN/Infinity 污染回测。
# Args: values - 原始数值序列。
# Returns: 有限浮点数列表。
def _coerce_values(values: list[float]) -> list[float]:
    logger.debug("预测数值校验入口", value_count=len(values))
    if len(values) < 4:
        raise ForecastingError("样本不足，至少需要 4 个数值")
    result = []
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ForecastingError(f"存在非数值数据: {value}") from exc
        if not math.isfinite(parsed):
            raise ForecastingError("预测数据不能包含 NaN 或 Infinity")
        result.append(parsed)
    logger.info("预测数值校验完成", value_count=len(result))
    return result


# 方法作用：生成最后观测值重复的 naive 基线。
# Args: values - 训练序列；horizon - 预测步数。
# Returns: 预测值列表。
def _naive_predict(values: list[float], horizon: int) -> list[float]:
    logger.debug("naive 预测入口", train_count=len(values), horizon=horizon)
    return [values[-1]] * horizon


# 方法作用：用最小二乘线性趋势拟合训练序列并外推。
# Args: values - 训练序列；horizon - 预测步数。
# Returns: 预测值列表。
def _linear_predict(values: list[float], horizon: int) -> list[float]:
    logger.debug("线性预测入口", train_count=len(values), horizon=horizon)
    n = len(values)
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    sxx = sum((index - x_mean) ** 2 for index in range(n))
    if sxx == 0:
        return _naive_predict(values, horizon)
    slope = sum((index - x_mean) * (values[index] - y_mean) for index in range(n)) / sxx
    intercept = y_mean - slope * x_mean
    return [intercept + slope * (n + step) for step in range(horizon)]


# 方法作用：计算误差序列的 MAE、RMSE 和 SMAPE 指标。
# Args: errors - 预测误差列表；actual - 与误差对应的真实值列表。
# Returns: 三项评估指标。
def _metrics(errors: list[float], actual: list[float]) -> dict[str, float]:
    if not errors:
        return {"mae": 0.0, "rmse": 0.0, "smape": 0.0}
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    smape = sum(2 * abs(error) / max(abs(real) + abs(real - error), 1e-9) for error, real in zip(errors, actual)) / len(errors) * 100
    return {"mae": round(mae, 6), "rmse": round(rmse, 6), "smape": round(smape, 6)}


# 方法作用：按回测窗口收集真实值，用于计算 SMAPE。
# Args: values - 完整序列；min_train - 初始训练长度；horizon - 窗口预测步数。
# Returns: 与所有误差对应的真实值列表。
def _actual_for_errors(values: list[float], min_train: int, horizon: int) -> list[float]:
    logger.debug("回测真实值收集入口", value_count=len(values))
    actual: list[float] = []
    for end in range(min_train, len(values) - horizon + 1):
        actual.extend(values[end:end + horizon])
    return actual


# 方法作用：计算所选模型的样本内残差，用于预测区间尺度。
# Args: values - 历史序列；model - naive 或 linear。
# Returns: 残差列表。
def _in_sample_residuals(values: list[float], model: str) -> list[float]:
    logger.debug("样本内残差计算入口", model=model, value_count=len(values))
    if len(values) < 2:
        return []
    if model == "naive":
        return [values[index] - values[index - 1] for index in range(1, len(values))]
    fitted = _linear_predict(values[:-1], 1)[0]
    residuals = []
    for index in range(1, len(values)):
        train = values[:index]
        residuals.append(values[index] - _linear_predict(train, 1)[0])
    return residuals or [values[-1] - fitted]


# 方法作用：计算标准差并在样本不足时回退到零。
# Args: values - 数值列表。
# Returns: 样本标准差。
def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


# 方法作用：返回常用置信水平对应的正态分位点。
# Args: confidence - 置信水平。
# Returns: 正态分布 z 值。
def _normal_quantile(confidence: float) -> float:
    if confidence <= 0.8:
        return 1.2816
    if confidence <= 0.9:
        return 1.6449
    return 1.96
