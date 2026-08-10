"""数据资产来源与变换链审计工具。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AuditProvenanceTool(BaseTool):
    """审计资产指纹、证据定位和有序变换链完整性。"""

    name: str = "audit_provenance"
    description: str = "检查资产指纹、来源定位、工具参数、代码版本和变换链是否完整可追溯。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("audit_provenance 只允许异步执行")

    async def _arun(
        self,
        assets: list[dict[str, Any]],
        transforms: list[dict[str, Any]],
        expected_output_asset_id: str | None = None,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """对调用方传入的来源记录执行离线一致性审计。"""
        del run_manager
        findings: list[dict[str, Any]] = []
        checks_total = 0
        checks_passed = 0

        def check(
            condition: bool,
            code: str,
            path: str,
            message: str,
            severity: str = "error",
        ) -> None:
            nonlocal checks_total, checks_passed
            checks_total += 1
            if condition:
                checks_passed += 1
            else:
                findings.append(
                    {
                        "code": code,
                        "severity": severity,
                        "path": path,
                        "message": message,
                    }
                )

        logger.info(
            "来源链审计开始",
            asset_count=len(assets),
            transform_count=len(transforms),
            expected_output_asset_id=expected_output_asset_id,
        )
        asset_map: dict[str, dict[str, Any]] = {}
        for index, asset in enumerate(assets):
            path = f"assets[{index}]"
            is_object = isinstance(asset, dict)
            check(is_object, "ASSET_INVALID", path, "资产记录必须是对象")
            if not is_object:
                continue
            asset_id = str(asset.get("asset_id", "")).strip()
            check(bool(asset_id), "ASSET_ID_MISSING", f"{path}.asset_id", "资产标识不能为空")
            if not asset_id:
                continue
            check(
                asset_id not in asset_map,
                "ASSET_DUPLICATE",
                f"{path}.asset_id",
                "资产标识重复",
            )
            if asset_id in asset_map:
                continue
            asset_map[asset_id] = asset
            fingerprint = str(asset.get("fingerprint", ""))
            check(
                bool(_SHA256_PATTERN.fullmatch(fingerprint)),
                "FINGERPRINT_INVALID",
                f"{path}.fingerprint",
                "资产指纹必须是 64 位小写 SHA-256",
            )
            source_id = str(asset.get("source_id", "")).strip()
            locator = asset.get("locator")
            if source_id or locator is not None:
                check(bool(source_id), "SOURCE_ID_MISSING", f"{path}.source_id", "来源标识不能为空")
                check(
                    isinstance(locator, dict) and bool(locator),
                    "LOCATOR_MISSING",
                    f"{path}.locator",
                    "来源资产必须提供非空证据定位",
                )

        produced_ids = {
            str(transform.get("output_asset_id", ""))
            for transform in transforms
            if isinstance(transform, dict) and str(transform.get("output_asset_id", ""))
        }
        available = set(asset_map) - produced_ids
        produced_seen: set[str] = set()
        step_ids: set[str] = set()
        for index, transform in enumerate(transforms):
            path = f"transforms[{index}]"
            is_object = isinstance(transform, dict)
            check(is_object, "TRANSFORM_INVALID", path, "变换记录必须是对象")
            if not is_object:
                continue
            step_id = str(transform.get("step_id", "")).strip()
            check(bool(step_id), "STEP_ID_MISSING", f"{path}.step_id", "步骤标识不能为空")
            check(
                bool(step_id) and step_id not in step_ids,
                "STEP_ID_DUPLICATE",
                f"{path}.step_id",
                "步骤标识重复",
            )
            if step_id:
                step_ids.add(step_id)
            tool_name = str(transform.get("tool", "")).strip()
            code_version = str(transform.get("code_version", "")).strip()
            parameters = transform.get("parameters")
            check(bool(tool_name), "TOOL_MISSING", f"{path}.tool", "变换工具名称不能为空")
            check(
                isinstance(parameters, dict),
                "PARAMETERS_MISSING",
                f"{path}.parameters",
                "变换参数必须完整记录为对象",
            )
            check(
                bool(code_version),
                "CODE_VERSION_MISSING",
                f"{path}.code_version",
                "代码版本不能为空",
            )
            input_ids = transform.get("input_asset_ids")
            valid_inputs = isinstance(input_ids, list) and bool(input_ids)
            check(
                valid_inputs,
                "INPUT_ASSETS_MISSING",
                f"{path}.input_asset_ids",
                "每个变换步骤至少需要一个输入资产",
            )
            normalized_inputs = [str(item) for item in input_ids] if valid_inputs else []
            for input_index, asset_id in enumerate(normalized_inputs):
                check(
                    asset_id in asset_map,
                    "INPUT_ASSET_UNKNOWN",
                    f"{path}.input_asset_ids[{input_index}]",
                    "输入资产未在资产清单中声明",
                )
                check(
                    asset_id in available,
                    "CHAIN_DISCONTINUITY",
                    f"{path}.input_asset_ids[{input_index}]",
                    "输入资产尚未在当前变换顺序中产生",
                )
            output_id = str(transform.get("output_asset_id", "")).strip()
            check(
                bool(output_id),
                "OUTPUT_ASSET_MISSING",
                f"{path}.output_asset_id",
                "输出资产不能为空",
            )
            if output_id:
                check(
                    output_id in asset_map,
                    "OUTPUT_ASSET_UNKNOWN",
                    f"{path}.output_asset_id",
                    "输出资产未在资产清单中声明",
                )
                check(
                    output_id not in produced_seen,
                    "OUTPUT_ASSET_DUPLICATE",
                    f"{path}.output_asset_id",
                    "同一资产不能由多个步骤重复产生",
                )
                produced_seen.add(output_id)
                available.add(output_id)
            evidence = transform.get("evidence", [])
            check(
                isinstance(evidence, list), "EVIDENCE_INVALID", f"{path}.evidence", "证据必须是数组"
            )
            if isinstance(evidence, list):
                for evidence_index, item in enumerate(evidence):
                    evidence_path = f"{path}.evidence[{evidence_index}]"
                    check(
                        isinstance(item, dict)
                        and bool(str(item.get("source_id", "")).strip())
                        and isinstance(item.get("locator"), dict)
                        and bool(item.get("locator")),
                        "EVIDENCE_LOCATOR_MISSING",
                        evidence_path,
                        "证据必须同时包含来源标识和非空定位",
                    )

        if expected_output_asset_id is not None:
            check(
                expected_output_asset_id in available,
                "EXPECTED_OUTPUT_UNAVAILABLE",
                "expected_output_asset_id",
                "预期输出资产未在完整变换链中产生",
            )
        terminal_assets = sorted(
            available
            - {
                str(item)
                for transform in transforms
                if isinstance(transform, dict)
                for item in (transform.get("input_asset_ids") or [])
            }
        )
        errors = sum(item["severity"] == "error" for item in findings)
        warnings = sum(item["severity"] == "warning" for item in findings)
        score = checks_passed / checks_total if checks_total else 1.0
        audit_source = json.dumps(
            {"assets": assets, "transforms": transforms},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        result = {
            "valid": errors == 0,
            "completeness_score": round(score, 6),
            "summary": {
                "checks_total": checks_total,
                "checks_passed": checks_passed,
                "errors": errors,
                "warnings": warnings,
            },
            "findings": findings,
            "terminal_asset_ids": terminal_assets,
            "audit_fingerprint": hashlib.sha256(audit_source.encode("utf-8")).hexdigest(),
            "filesystem_accessed": False,
        }
        logger.info(
            "来源链审计完成",
            valid=result["valid"],
            completeness_score=result["completeness_score"],
            finding_count=len(findings),
        )
        return result


_TOOLS: dict[str, BaseTool] = {"audit_provenance": AuditProvenanceTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
