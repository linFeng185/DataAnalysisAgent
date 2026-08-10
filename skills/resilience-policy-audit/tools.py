"""超时、重试、熔断、幂等和补偿策略审计工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_BACKOFFS = {"none", "fixed", "exponential"}


class AuditResiliencePolicyTool(BaseTool):
    """对调用方声明的可靠性策略执行确定性门禁。"""

    name: str = "audit_resilience_policy"
    description: str = "审核操作的超时、重试、退避、熔断、幂等键、错误分类和失败补偿策略。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("audit_resilience_policy 只允许异步执行")

    async def _arun(
        self,
        policies: list[dict[str, Any]],
        maximum_timeout_ms: float = 30000.0,
        maximum_total_time_ms: float = 120000.0,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """计算最坏耗时并检查重试与副作用安全边界。"""
        del run_manager
        if not policies:
            raise ValueError("policies 必须包含至少一条策略")
        if maximum_timeout_ms <= 0 or maximum_total_time_ms <= 0:
            raise ValueError("超时预算必须大于零")
        logger.info(
            "韧性策略审计开始",
            policy_count=len(policies),
            maximum_timeout_ms=maximum_timeout_ms,
            maximum_total_time_ms=maximum_total_time_ms,
        )
        all_findings: list[dict[str, Any]] = []
        policy_results: list[dict[str, Any]] = []
        operations: set[str] = set()
        for index, policy in enumerate(policies):
            normalized = _normalize_policy(policy, index)
            operation = normalized["operation"]
            if operation in operations:
                raise ValueError(f"操作策略重复: {operation}")
            operations.add(operation)
            findings: list[dict[str, Any]] = []

            def add(
                code: str,
                severity: str,
                message: str,
                current_operation: str = operation,
                current_findings: list[dict[str, Any]] = findings,
            ) -> None:
                item = {
                    "operation": current_operation,
                    "code": code,
                    "severity": severity,
                    "message": message,
                }
                current_findings.append(item)
                all_findings.append(item)

            timeout = normalized["timeout_ms"]
            attempts = normalized["max_attempts"]
            if timeout > maximum_timeout_ms:
                add("TIMEOUT_EXCEEDED", "error", "单次超时超过允许预算")
            if attempts > 1:
                if normalized["backoff"] == "none":
                    add("BACKOFF_MISSING", "error", "多次尝试必须配置固定或指数退避")
                if normalized["initial_delay_ms"] <= 0:
                    add("INITIAL_DELAY_MISSING", "error", "多次尝试必须配置正数初始等待")
                if normalized["max_delay_ms"] < normalized["initial_delay_ms"]:
                    add("MAX_DELAY_INVALID", "error", "最大等待不能小于初始等待")
                if not normalized["retryable_errors"]:
                    add("RETRYABLE_ERRORS_MISSING", "error", "重试策略必须明确可重试错误")
                if "*" in normalized["retryable_errors"]:
                    add("RETRY_WILDCARD", "error", "禁止对所有错误执行无差别重试")
                overlap = set(normalized["retryable_errors"]) & set(
                    normalized["non_retryable_errors"]
                )
                if overlap:
                    add("ERROR_CLASS_OVERLAP", "error", "同一错误不能同时声明为可重试和不可重试")
                if not normalized["jitter"] and normalized["remote"]:
                    add("JITTER_MISSING", "warning", "远程操作重试建议加入随机抖动以避免同步风暴")
            if normalized["side_effecting"] and attempts > 1:
                if not normalized["idempotent"] and not normalized["idempotency_key_required"]:
                    add("IDEMPOTENCY_MISSING", "error", "有副作用重试必须幂等或强制幂等键")
                if not normalized["idempotent"] and not normalized["compensation_enabled"]:
                    add("COMPENSATION_MISSING", "error", "非幂等副作用操作必须声明失败补偿")
            if normalized["remote"] and normalized["circuit_breaker"] is None:
                add("CIRCUIT_BREAKER_MISSING", "warning", "远程操作建议配置熔断器")
            if normalized["circuit_breaker"] is not None:
                breaker = normalized["circuit_breaker"]
                if (
                    breaker["failure_threshold"] < 1
                    or breaker["recovery_timeout_ms"] <= 0
                    or breaker["half_open_max_calls"] < 1
                ):
                    add(
                        "CIRCUIT_BREAKER_INVALID",
                        "error",
                        "熔断阈值、恢复时间和半开探测次数必须为正数",
                    )

            total_delay = _total_delay(normalized)
            worst_case = attempts * timeout + total_delay
            if worst_case > maximum_total_time_ms:
                add("TOTAL_BUDGET_EXCEEDED", "error", "最坏重试耗时超过操作总预算")
            errors = sum(item["severity"] == "error" for item in findings)
            warnings = sum(item["severity"] == "warning" for item in findings)
            policy_results.append(
                {
                    "operation": operation,
                    "valid": errors == 0,
                    "worst_case_duration_ms": round(worst_case, 6),
                    "total_backoff_ms": round(total_delay, 6),
                    "errors": errors,
                    "warnings": warnings,
                }
            )

        errors = sum(item["severity"] == "error" for item in all_findings)
        warnings = sum(item["severity"] == "warning" for item in all_findings)
        result = {
            "valid": errors == 0,
            "score": max(0, 100 - errors * 20 - warnings * 5),
            "summary": {
                "policy_count": len(policies),
                "valid_policies": sum(item["valid"] for item in policy_results),
                "errors": errors,
                "warnings": warnings,
            },
            "policy_results": policy_results,
            "findings": all_findings,
            "external_actions_executed": False,
        }
        logger.info(
            "韧性策略审计完成",
            valid=result["valid"],
            score=result["score"],
            error_count=errors,
            warning_count=warnings,
        )
        return result


def _normalize_policy(policy: Any, index: int) -> dict[str, Any]:
    """校验并规范化单条韧性策略。"""
    if not isinstance(policy, dict):
        raise ValueError(f"policies 第 {index} 项必须是对象")
    operation = str(policy.get("operation", "")).strip()
    backoff = str(policy.get("backoff", "none"))
    timeout_ms = policy.get("timeout_ms")
    max_attempts = policy.get("max_attempts", 1)
    initial_delay_ms = policy.get("initial_delay_ms", 0)
    max_delay_ms = policy.get("max_delay_ms", 0)
    if not operation:
        raise ValueError(f"policies 第 {index} 项缺少 operation")
    if backoff not in _BACKOFFS:
        raise ValueError(f"不支持的退避策略: {backoff}")
    if not _positive_number(timeout_ms):
        raise ValueError(f"操作 {operation} 的 timeout_ms 必须是正数")
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or not 1 <= max_attempts <= 10
    ):
        raise ValueError(f"操作 {operation} 的 max_attempts 必须在 1 到 10 之间")
    if not _non_negative_number(initial_delay_ms) or not _non_negative_number(max_delay_ms):
        raise ValueError(f"操作 {operation} 的等待时间必须是非负数")
    retryable = policy.get("retryable_errors") or []
    non_retryable = policy.get("non_retryable_errors") or []
    if any(not isinstance(items, list) for items in (retryable, non_retryable)):
        raise ValueError(f"操作 {operation} 的错误分类必须是数组")
    breaker = policy.get("circuit_breaker")
    if breaker is not None:
        if not isinstance(breaker, dict):
            raise ValueError(f"操作 {operation} 的 circuit_breaker 必须是对象或空值")
        breaker = {
            "failure_threshold": breaker.get("failure_threshold", 0),
            "recovery_timeout_ms": breaker.get("recovery_timeout_ms", 0),
            "half_open_max_calls": breaker.get("half_open_max_calls", 0),
        }
        if not isinstance(breaker["failure_threshold"], int) or isinstance(
            breaker["failure_threshold"], bool
        ):
            raise ValueError(f"操作 {operation} 的 failure_threshold 必须是整数")
        if not _non_negative_number(breaker["recovery_timeout_ms"]):
            raise ValueError(f"操作 {operation} 的 recovery_timeout_ms 必须是非负数")
        if not isinstance(breaker["half_open_max_calls"], int) or isinstance(
            breaker["half_open_max_calls"], bool
        ):
            raise ValueError(f"操作 {operation} 的 half_open_max_calls 必须是整数")
    return {
        "operation": operation,
        "timeout_ms": float(timeout_ms),
        "max_attempts": max_attempts,
        "backoff": backoff,
        "initial_delay_ms": float(initial_delay_ms),
        "max_delay_ms": float(max_delay_ms),
        "jitter": bool(policy.get("jitter", False)),
        "remote": bool(policy.get("remote", False)),
        "side_effecting": bool(policy.get("side_effecting", False)),
        "idempotent": bool(policy.get("idempotent", False)),
        "idempotency_key_required": bool(policy.get("idempotency_key_required", False)),
        "compensation_enabled": bool(policy.get("compensation_enabled", False)),
        "retryable_errors": [str(item) for item in retryable],
        "non_retryable_errors": [str(item) for item in non_retryable],
        "circuit_breaker": breaker,
    }


def _total_delay(policy: dict[str, Any]) -> float:
    """计算所有重试等待的最坏总时长。"""
    retries = policy["max_attempts"] - 1
    if retries <= 0 or policy["backoff"] == "none":
        return 0.0
    total = 0.0
    for retry_index in range(retries):
        delay = policy["initial_delay_ms"]
        if policy["backoff"] == "exponential":
            delay *= 2**retry_index
        if policy["max_delay_ms"] > 0:
            delay = min(delay, policy["max_delay_ms"])
        if policy["jitter"]:
            delay *= 2
        total += delay
    return total


def _positive_number(value: Any) -> bool:
    """判断值是否为正数且排除布尔值。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _non_negative_number(value: Any) -> bool:
    """判断值是否为非负数且排除布尔值。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


_TOOLS: dict[str, BaseTool] = {"audit_resilience_policy": AuditResiliencePolicyTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
