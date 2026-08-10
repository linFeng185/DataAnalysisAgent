"""结论与证据引用覆盖率审计工具。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuditEvidenceCoverageTool(BaseTool):
    """审核主张引用是否存在、可定位且具备独立来源覆盖。"""

    name: str = "audit_evidence_coverage"
    description: str = "审核结论与引用映射、证据定位、内容指纹和关键主张的独立来源覆盖率。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("audit_evidence_coverage 只允许异步执行")

    async def _arun(
        self,
        claims: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        minimum_citations: int = 1,
        critical_minimum_citations: int = 2,
        minimum_coverage: float = 0.95,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """按主张重要性和独立来源数计算证据覆盖率。"""
        del run_manager
        if not 1 <= minimum_citations <= 10:
            raise ValueError("minimum_citations 必须在 1 到 10 之间")
        if not minimum_citations <= critical_minimum_citations <= 10:
            raise ValueError("critical_minimum_citations 不能低于普通主张要求且不能超过 10")
        if not 0 <= minimum_coverage <= 1:
            raise ValueError("minimum_coverage 必须在 0 到 1 之间")
        logger.info(
            "证据覆盖审计开始",
            claim_count=len(claims),
            evidence_count=len(evidence),
            minimum_coverage=minimum_coverage,
        )
        evidence_map: dict[str, dict[str, Any]] = {}
        evidence_issues: list[dict[str, Any]] = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValueError(f"evidence 第 {index} 项必须是对象")
            evidence_id = str(item.get("evidence_id", "")).strip()
            source_id = str(item.get("source_id", "")).strip()
            locator = item.get("locator")
            content_hash = str(item.get("content_hash", ""))
            if not evidence_id or not source_id:
                raise ValueError(f"evidence 第 {index} 项必须提供 evidence_id 和 source_id")
            if evidence_id in evidence_map:
                raise ValueError(f"证据标识重复: {evidence_id}")
            valid = True
            if not isinstance(locator, dict) or not locator:
                valid = False
                evidence_issues.append(
                    {
                        "evidence_id": evidence_id,
                        "code": "LOCATOR_MISSING",
                        "message": "证据缺少非空来源定位",
                    }
                )
            if not _SHA256.fullmatch(content_hash):
                valid = False
                evidence_issues.append(
                    {
                        "evidence_id": evidence_id,
                        "code": "CONTENT_HASH_INVALID",
                        "message": "证据内容指纹不是 64 位小写 SHA-256",
                    }
                )
            evidence_map[evidence_id] = {
                "source_id": source_id,
                "valid": valid,
            }

        claim_results: list[dict[str, Any]] = []
        cited_evidence: Counter[str] = Counter()
        invalid_references: list[dict[str, str]] = []
        total_weight = 0
        covered_weight = 0
        claim_ids: set[str] = set()
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                raise ValueError(f"claims 第 {index} 项必须是对象")
            claim_id = str(claim.get("claim_id", "")).strip()
            claim_hash = str(claim.get("claim_hash", ""))
            critical = bool(claim.get("critical", False))
            citation_ids = claim.get("citation_ids") or []
            if not claim_id or not _SHA256.fullmatch(claim_hash):
                raise ValueError(f"claims 第 {index} 项必须提供 claim_id 和合法 claim_hash")
            if claim_id in claim_ids:
                raise ValueError(f"主张标识重复: {claim_id}")
            claim_ids.add(claim_id)
            if not isinstance(citation_ids, list) or len(set(citation_ids)) != len(citation_ids):
                raise ValueError(f"主张 {claim_id} 的 citation_ids 必须是无重复数组")
            valid_ids: list[str] = []
            source_ids: set[str] = set()
            for citation_id in citation_ids:
                normalized_id = str(citation_id)
                item = evidence_map.get(normalized_id)
                if item is None:
                    invalid_references.append(
                        {
                            "claim_id": claim_id,
                            "evidence_id": normalized_id,
                            "message": "引用的证据不存在",
                        }
                    )
                    continue
                cited_evidence[normalized_id] += 1
                if item["valid"]:
                    valid_ids.append(normalized_id)
                    source_ids.add(item["source_id"])
            required = critical_minimum_citations if critical else minimum_citations
            covered = len(valid_ids) >= required and len(source_ids) >= required
            weight = 2 if critical else 1
            total_weight += weight
            if covered:
                covered_weight += weight
            claim_results.append(
                {
                    "claim_id": claim_id,
                    "critical": critical,
                    "covered": covered,
                    "required_citations": required,
                    "valid_citations": len(valid_ids),
                    "independent_sources": len(source_ids),
                    "invalid_citations": len(citation_ids) - len(valid_ids),
                }
            )

        coverage = covered_weight / total_weight if total_weight else 1.0
        uncovered = [item["claim_id"] for item in claim_results if not item["covered"]]
        orphan_evidence = sorted(set(evidence_map) - set(cited_evidence))
        gate_passed = (
            coverage >= minimum_coverage
            and not uncovered
            and not invalid_references
            and not evidence_issues
        )
        result = {
            "gate_passed": gate_passed,
            "coverage_score": round(coverage, 6),
            "minimum_coverage": minimum_coverage,
            "summary": {
                "claim_count": len(claims),
                "covered_claims": len(claims) - len(uncovered),
                "uncovered_claims": len(uncovered),
                "evidence_count": len(evidence),
                "invalid_references": len(invalid_references),
                "evidence_issues": len(evidence_issues),
            },
            "claim_results": claim_results,
            "uncovered_claim_ids": uncovered,
            "invalid_references": invalid_references,
            "evidence_issues": evidence_issues,
            "orphan_evidence_ids": orphan_evidence,
            "raw_claims_returned": False,
        }
        logger.info(
            "证据覆盖审计完成",
            gate_passed=gate_passed,
            coverage_score=result["coverage_score"],
            uncovered_claim_count=len(uncovered),
            invalid_reference_count=len(invalid_references),
        )
        return result


_TOOLS: dict[str, BaseTool] = {"audit_evidence_coverage": AuditEvidenceCoverageTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
