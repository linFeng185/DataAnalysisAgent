"""Phase E 情景规划测试。"""

from __future__ import annotations

import pytest


class TestScenarioPlanning:
    """覆盖情景组合、约束校验和资源上限。"""

    def test_generate_scenarios_returns_feasible_ranked_options(self):
        """情景规划应只返回满足约束的候选，并按评分排序。"""
        # Arrange
        from src.tools.scenario_planning import generate_scenarios

        # Act
        result = generate_scenarios(
            baseline={"demand": 100, "stock": 100},
            variables={"demand": [100, 120], "stock": [100, 130]},
            objective="maximize_service",
            constraints={"stock": {"min": 100, "max": 120}},
            evaluator=lambda state: state["stock"] - abs(state["demand"] - state["stock"]),
        )

        # Assert
        assert result.objective == "maximize_service"
        assert result.feasible_count == 2
        assert result.scenarios[0].score >= result.scenarios[-1].score
        assert all(item.constraints_passed for item in result.scenarios)

    def test_generate_scenarios_rejects_excessive_combination_count(self):
        """组合数量超过资源预算时必须停止，而不是无限笛卡尔积。"""
        from src.tools.scenario_planning import ScenarioPlanningError, generate_scenarios

        with pytest.raises(ScenarioPlanningError, match="组合数量"):
            generate_scenarios(
                baseline={"x": 0},
                variables={"x": list(range(20)), "y": list(range(20))},
                objective="test",
                max_scenarios=10,
            )
