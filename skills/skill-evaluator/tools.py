"""基于调用记录的离线 Skill 质量评测工具。"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class EvaluateSkillRunsTool(BaseTool):
    """评估既有执行记录的契约、期望、确定性和耗时。"""

    name: str = "evaluate_skill_runs"
    description: str = "离线评估调用方提供的 Skill 执行记录，不加载或执行任意待测代码。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("evaluate_skill_runs 只允许异步执行")

    async def _arun(
        self,
        cases: list[dict[str, Any]],
        minimum_score: float = 0.95,
        maximum_duration_ms: float = 1000.0,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """对固定执行记录应用四类质量门禁。"""
        del run_manager
        if not cases:
            raise ValueError("cases 必须包含至少一条执行记录")
        if not 0 <= minimum_score <= 1:
            raise ValueError("minimum_score 必须在 0 到 1 之间")
        if maximum_duration_ms <= 0:
            raise ValueError("maximum_duration_ms 必须大于零")
        logger.info(
            "Skill 离线评测开始",
            case_count=len(cases),
            minimum_score=minimum_score,
            maximum_duration_ms=maximum_duration_ms,
        )
        case_results: list[dict[str, Any]] = []
        nondeterministic_cases: list[str] = []
        schema_failures: list[str] = []
        slow_cases: list[str] = []

        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                raise ValueError(f"第 {index} 条执行记录必须是对象")
            case_id = str(case.get("case_id", "")).strip()
            if not case_id:
                raise ValueError(f"第 {index} 条执行记录缺少 case_id")
            output_schema = case.get("output_schema")
            if not isinstance(output_schema, dict):
                raise ValueError(f"执行记录 {case_id} 的 output_schema 必须是对象")
            output = case.get("output")
            expected_subset = case.get("expected_subset", {})
            repeat_outputs = case.get("repeat_outputs") or []
            duration_ms = case.get("duration_ms")
            if not isinstance(expected_subset, dict):
                raise ValueError(f"执行记录 {case_id} 的 expected_subset 必须是对象")
            if not isinstance(repeat_outputs, list):
                raise ValueError(f"执行记录 {case_id} 的 repeat_outputs 必须是数组")
            if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
                raise ValueError(f"执行记录 {case_id} 的 duration_ms 必须是数值")

            checks: list[dict[str, Any]] = []
            schema_passed, schema_message = _validate_output(output, output_schema)
            checks.append(_check_result("output_schema", schema_passed, schema_message))
            expected_passed = _contains_subset(output, expected_subset)
            checks.append(
                _check_result(
                    "expected_subset",
                    expected_passed,
                    "输出包含期望子集" if expected_passed else "输出不包含完整期望子集",
                )
            )
            output_token = _token(output)
            deterministic = all(_token(item) == output_token for item in repeat_outputs)
            checks.append(
                _check_result(
                    "determinism",
                    deterministic,
                    "重复输出一致" if deterministic else "重复输出不一致",
                )
            )
            within_budget = 0 <= float(duration_ms) <= maximum_duration_ms
            checks.append(
                _check_result(
                    "duration_budget",
                    within_budget,
                    "耗时符合预算" if within_budget else "耗时超过预算或为负数",
                )
            )
            error = str(case.get("error") or "").strip()
            if error:
                checks.append(_check_result("execution", False, "执行记录包含错误"))

            passed_checks = sum(item["passed"] for item in checks)
            score = passed_checks / len(checks)
            passed = score >= minimum_score
            case_results.append(
                {
                    "case_id": case_id,
                    "passed": passed,
                    "score": round(score, 6),
                    "checks": checks,
                }
            )
            if not deterministic:
                nondeterministic_cases.append(case_id)
            if not schema_passed:
                schema_failures.append(case_id)
            if not within_budget:
                slow_cases.append(case_id)

        passed_cases = sum(item["passed"] for item in case_results)
        average_score = sum(item["score"] for item in case_results) / len(case_results)
        result = {
            "gate_passed": passed_cases == len(case_results) and average_score >= minimum_score,
            "minimum_score": minimum_score,
            "average_score": round(average_score, 6),
            "summary": {
                "total_cases": len(case_results),
                "passed_cases": passed_cases,
                "failed_cases": len(case_results) - passed_cases,
            },
            "case_results": case_results,
            "nondeterministic_cases": nondeterministic_cases,
            "schema_failures": schema_failures,
            "slow_cases": slow_cases,
            "filesystem_accessed": False,
            "untrusted_code_executed": False,
        }
        logger.info(
            "Skill 离线评测完成",
            gate_passed=result["gate_passed"],
            average_score=result["average_score"],
            failed_cases=result["summary"]["failed_cases"],
        )
        return result


def _validate_output(output: Any, schema: dict[str, Any]) -> tuple[bool, str]:
    """校验内联输出 Schema 及输出实例。"""
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(output)
    except SchemaError as exc:
        return False, f"输出 Schema 无效: {exc.message}"
    except ValidationError as exc:
        path = ".".join(str(item) for item in exc.absolute_path)
        location = path or "根节点"
        return False, f"输出在 {location} 不符合 Schema: {exc.message}"
    return True, "输出符合 Schema"


def _contains_subset(actual: Any, expected: Any) -> bool:
    """递归判断输出是否包含期望子集。"""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains_subset(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return all(
            any(_contains_subset(candidate, item) for candidate in actual) for item in expected
        )
    return _token(actual) == _token(expected)


def _check_result(name: str, passed: bool, message: str) -> dict[str, Any]:
    """构造统一评测检查结果。"""
    return {"name": name, "passed": passed, "message": message}


def _token(value: Any) -> str:
    """把任意 JSON 兼容值转换为稳定比较标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"evaluate_skill_runs": EvaluateSkillRunsTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
