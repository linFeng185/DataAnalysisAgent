"""受约束情景组合与方案排序。"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable

from src.logging_config import get_logger

logger = get_logger(__name__)


class ScenarioPlanningError(ValueError):
    """情景规划输入或资源预算不满足时抛出的异常。"""


@dataclass
class ScenarioOption:
    """一个通过约束校验的候选方案。"""

    values: dict[str, Any]
    score: float
    constraints_passed: bool
    violations: list[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    """情景规划输出，包含基准、候选和约束摘要。"""

    objective: str
    baseline: dict[str, Any]
    scenarios: list[ScenarioOption]
    feasible_count: int
    evaluated_count: int
    constraints: dict[str, Any]

    # 方法作用：把方案结果转换成 JSON 兼容结构。
    # Args: self - 情景规划结果。
    # Returns: 情景规划字典。
    def to_dict(self) -> dict[str, Any]:
        logger.debug("情景结果序列化入口", objective=self.objective)
        result = {
            "objective": self.objective,
            "baseline": self.baseline,
            "scenarios": [
                {"values": item.values, "score": item.score,
                 "constraints_passed": item.constraints_passed, "violations": item.violations}
                for item in self.scenarios
            ],
            "feasible_count": self.feasible_count,
            "evaluated_count": self.evaluated_count,
            "constraints": self.constraints,
        }
        logger.info("情景结果序列化完成", scenarios=len(self.scenarios))
        return result


# 方法作用：生成变量笛卡尔积，校验每个候选的上下界约束并排序。
# Args: baseline - 基准变量；variables - 可控变量及候选值；objective - 目标名称；constraints - min/max 约束；evaluator - 可选评分函数；max_scenarios - 最大组合数。
# Returns: 按 score 降序排列的 ScenarioResult。
def generate_scenarios(
    baseline: dict[str, Any],
    variables: dict[str, list[Any]],
    objective: str,
    constraints: dict[str, dict[str, float]] | None = None,
    evaluator: Callable[[dict[str, Any]], float] | None = None,
    max_scenarios: int = 1000,
) -> ScenarioResult:
    logger.debug("情景规划入口", variables=list(variables), max_scenarios=max_scenarios)
    if not objective.strip():
        raise ScenarioPlanningError("objective 不能为空")
    if not variables:
        raise ScenarioPlanningError("至少需要一个可控变量")
    if max_scenarios <= 0:
        raise ScenarioPlanningError("max_scenarios 必须大于零")
    names = list(variables)
    options = [values for values in variables.values()]
    if any(not values for values in options):
        raise ScenarioPlanningError("变量候选值不能为空")
    total = 1
    for values in options:
        total *= len(values)
    if total > max_scenarios:
        raise ScenarioPlanningError(f"组合数量 {total} 超过上限 {max_scenarios}")
    rules = constraints or {}
    evaluated: list[ScenarioOption] = []
    for combination in itertools.product(*options):
        state = dict(baseline)
        state.update(dict(zip(names, combination)))
        violations = _constraint_violations(state, rules)
        if violations:
            continue
        try:
            score = float(evaluator(state)) if evaluator else 0.0
        except (TypeError, ValueError) as exc:
            raise ScenarioPlanningError(f"方案评分失败: {exc}") from exc
        evaluated.append(ScenarioOption(values=state, score=round(score, 8), constraints_passed=True))
    evaluated.sort(key=lambda item: item.score, reverse=True)
    result = ScenarioResult(
        objective=objective,
        baseline=dict(baseline),
        scenarios=evaluated,
        feasible_count=len(evaluated),
        evaluated_count=total,
        constraints=rules,
    )
    logger.info("情景规划完成", evaluated=total, feasible=len(evaluated), objective=objective)
    return result


# 方法作用：检查候选变量是否违反声明的最小值或最大值。
# Args: state - 候选方案变量；constraints - 变量约束映射。
# Returns: 违规说明列表。
def _constraint_violations(state: dict[str, Any], constraints: dict[str, dict[str, float]]) -> list[str]:
    logger.debug("情景约束校验入口", constraints=list(constraints))
    violations: list[str] = []
    for name, rule in constraints.items():
        if name not in state:
            violations.append(f"缺少变量: {name}")
            continue
        value = state[name]
        try:
            number = float(value)
        except (TypeError, ValueError):
            violations.append(f"变量 {name} 不是数值")
            continue
        if "min" in rule and number < float(rule["min"]):
            violations.append(f"{name} 小于最小值")
        if "max" in rule and number > float(rule["max"]):
            violations.append(f"{name} 大于最大值")
    logger.info("情景约束校验完成", violations=len(violations))
    return violations
