"""执行结果可复现条件审计工具。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuditReproducibilityTool(BaseTool):
    """比较多次执行的输入、代码、依赖、配置、环境、种子和输出。"""

    name: str = "audit_reproducibility"
    description: str = "审核多次执行是否具备相同输入、代码、依赖、配置、环境、随机种子和输出。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("audit_reproducibility 只允许异步执行")

    async def _arun(
        self,
        runs: list[dict[str, Any]],
        minimum_runs: int = 2,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """按第一条执行记录为基准审计复现条件。"""
        del run_manager
        if not 2 <= minimum_runs <= 100:
            raise ValueError("minimum_runs 必须在 2 到 100 之间")
        normalized = [_normalize_run(run, index) for index, run in enumerate(runs)]
        logger.info(
            "可复现性审计开始",
            run_count=len(normalized),
            minimum_runs=minimum_runs,
        )
        findings: list[dict[str, Any]] = []
        run_ids = [run["run_id"] for run in normalized]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("runs 中的 run_id 不能重复")
        if len(normalized) < minimum_runs:
            findings.append(
                {
                    "code": "INSUFFICIENT_RUNS",
                    "severity": "error",
                    "run_id": "",
                    "field": "runs",
                    "message": "执行记录数量不足以验证可复现性",
                }
            )

        context_fields = [
            "input_fingerprint",
            "code_version",
            "dependencies",
            "config_fingerprint",
            "environment",
            "random_seed",
        ]
        baseline = normalized[0] if normalized else None
        context_consistent = bool(baseline)
        output_consistent = bool(baseline)
        for run in normalized[1:]:
            for field in context_fields:
                if _token(run[field]) == _token(baseline[field]):
                    continue
                context_consistent = False
                findings.append(
                    {
                        "code": f"{field.upper()}_DRIFT",
                        "severity": "error",
                        "run_id": run["run_id"],
                        "field": field,
                        "message": _context_message(field),
                    }
                )
            if run["output_fingerprint"] != baseline["output_fingerprint"]:
                output_consistent = False
                findings.append(
                    {
                        "code": "OUTPUT_DRIFT",
                        "severity": "error",
                        "run_id": run["run_id"],
                        "field": "output_fingerprint",
                        "message": "重复执行输出指纹不一致",
                    }
                )

        comparable_runs = (
            sum(
                all(_token(run[field]) == _token(baseline[field]) for field in context_fields)
                for run in normalized
            )
            if baseline
            else 0
        )
        audit_source = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        result = {
            "reproducible": len(normalized) >= minimum_runs
            and context_consistent
            and output_consistent,
            "context_consistent": context_consistent,
            "output_consistent": output_consistent,
            "summary": {
                "run_count": len(normalized),
                "minimum_runs": minimum_runs,
                "comparable_runs": comparable_runs,
                "finding_count": len(findings),
            },
            "baseline_run_id": baseline["run_id"] if baseline else "",
            "findings": findings,
            "audit_fingerprint": hashlib.sha256(audit_source.encode("utf-8")).hexdigest(),
            "filesystem_accessed": False,
        }
        logger.info(
            "可复现性审计完成",
            reproducible=result["reproducible"],
            context_consistent=context_consistent,
            output_consistent=output_consistent,
            finding_count=len(findings),
        )
        return result


def _normalize_run(run: Any, index: int) -> dict[str, Any]:
    """校验并规范化单条执行记录。"""
    if not isinstance(run, dict):
        raise ValueError(f"runs 第 {index} 项必须是对象")
    run_id = str(run.get("run_id", "")).strip()
    code_version = str(run.get("code_version", "")).strip()
    input_fingerprint = str(run.get("input_fingerprint", ""))
    config_fingerprint = str(run.get("config_fingerprint", ""))
    output_fingerprint = str(run.get("output_fingerprint", ""))
    dependencies = run.get("dependencies")
    environment = run.get("environment")
    random_seed = run.get("random_seed")
    if not run_id or not code_version:
        raise ValueError(f"runs 第 {index} 项必须提供 run_id 和 code_version")
    for field, value in (
        ("input_fingerprint", input_fingerprint),
        ("config_fingerprint", config_fingerprint),
        ("output_fingerprint", output_fingerprint),
    ):
        if not _SHA256.fullmatch(value):
            raise ValueError(f"runs 第 {index} 项的 {field} 必须是 64 位小写 SHA-256")
    if not isinstance(dependencies, dict) or any(
        not str(key).strip() or not str(value).strip() for key, value in dependencies.items()
    ):
        raise ValueError(f"runs 第 {index} 项的 dependencies 必须记录依赖名称和版本")
    if not isinstance(environment, dict) or not environment:
        raise ValueError(f"runs 第 {index} 项的 environment 必须是非空对象")
    if random_seed is not None and (
        not isinstance(random_seed, int) or isinstance(random_seed, bool)
    ):
        raise ValueError(f"runs 第 {index} 项的 random_seed 必须是整数或空值")
    return {
        "run_id": run_id,
        "input_fingerprint": input_fingerprint,
        "code_version": code_version,
        "dependencies": dict(sorted((str(key), str(value)) for key, value in dependencies.items())),
        "config_fingerprint": config_fingerprint,
        "environment": environment,
        "random_seed": random_seed,
        "output_fingerprint": output_fingerprint,
    }


def _context_message(field: str) -> str:
    """返回复现上下文字段变化说明。"""
    return {
        "input_fingerprint": "执行输入与基准记录不一致",
        "code_version": "代码版本与基准记录不一致",
        "dependencies": "依赖版本集合与基准记录不一致",
        "config_fingerprint": "配置指纹与基准记录不一致",
        "environment": "运行环境与基准记录不一致",
        "random_seed": "随机种子与基准记录不一致",
    }[field]


def _token(value: Any) -> str:
    """把记录值转换为稳定比较标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"audit_reproducibility": AuditReproducibilityTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
