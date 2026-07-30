"""Schema Top-K 召回率离线评估器。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SchemaRecallCase(BaseModel):
    """一条问题到期望表名集合的召回标注。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_tables: list[str] = Field(min_length=1)


def evaluate_schema_recall(
    cases: list[SchemaRecallCase | dict[str, Any]],
    retrieved_by_case: dict[str, list[str]],
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """计算逐表宏平均 Recall@K 和整例完全命中率。"""
    if top_k <= 0:
        raise ValueError("top_k 必须大于 0")
    normalized = [
        case if isinstance(case, SchemaRecallCase) else SchemaRecallCase.model_validate(case)
        for case in cases
    ]
    details: list[dict[str, Any]] = []
    recall_sum = 0.0
    complete_hits = 0
    for case in normalized:
        expected = set(case.expected_tables)
        retrieved = list(retrieved_by_case.get(case.case_id, []))[:top_k]
        hits = expected & set(retrieved)
        recall = len(hits) / len(expected)
        recall_sum += recall
        complete_hits += int(hits == expected)
        details.append({
            "case_id": case.case_id,
            "expected": sorted(expected),
            "retrieved": retrieved,
            "hits": sorted(hits),
            "recall_at_k": recall,
        })
    total = len(normalized)
    return {
        "case_count": total,
        "top_k": top_k,
        "recall_at_k": recall_sum / total if total else 1.0,
        "complete_hit_rate": complete_hits / total if total else 1.0,
        "details": details,
    }
