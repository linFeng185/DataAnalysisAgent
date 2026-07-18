"""知识检索离线评测指标。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class RetrievalEvaluationReport:
    """检索质量和安全评测结果。"""

    recall_at_k: float
    mrr_at_k: float
    citation_hit_rate: float
    unauthorized_hits: int
    case_count: int
    answered_case_count: int
    top_k: int

    # 方法作用：将评测报告转换为 JSON 兼容字典。
    # Args: self - 评测报告对象。
    # Returns: 评测指标字典。
    def to_dict(self) -> dict[str, Any]:
        logger.debug("检索评测报告序列化入口", case_count=self.case_count)
        result = self.__dict__.copy()
        logger.info("检索评测报告序列化完成", recall=self.recall_at_k, mrr=self.mrr_at_k)
        return result


# 方法作用：根据标注相关 ID 和实际召回 ID 计算检索质量与越权指标。
# Args: cases - 含 query/relevant_ids/authorized_ids 的标注用例；retrieved - 查询到的 ID 列表映射；top_k - 评测截断位置。
# Returns: RetrievalEvaluationReport。
def evaluate_retrieval_cases(
    cases: list[dict[str, Any]], retrieved: dict[str, list[str]], top_k: int = 5,
) -> RetrievalEvaluationReport:
    logger.debug("检索评测入口", cases=len(cases), top_k=top_k)
    if top_k <= 0:
        raise ValueError("top_k 必须大于零")
    recall_values: list[float] = []
    reciprocal_values: list[float] = []
    citation_hits = 0
    answered = 0
    unauthorized = 0
    for case in cases:
        query = str(case.get("query", ""))
        relevant = {str(value) for value in (case.get("relevant_ids", []) or [])}
        ids = [str(value) for value in (retrieved.get(query, []) or [])[:top_k]]
        authorized_values = case.get("authorized_ids")
        if authorized_values is not None:
            authorized = {str(value) for value in (authorized_values or [])}
            unauthorized += sum(1 for item_id in ids if item_id not in authorized)
        if not relevant:
            continue
        answered += 1
        hits = relevant & set(ids)
        recall_values.append(len(hits) / len(relevant))
        if hits:
            citation_hits += 1
            first_rank = next(index for index, item_id in enumerate(ids, start=1) if item_id in relevant)
            reciprocal_values.append(1 / first_rank)
        else:
            reciprocal_values.append(0.0)
    report = RetrievalEvaluationReport(
        recall_at_k=sum(recall_values) / len(cases) if cases else 0.0,
        mrr_at_k=sum(reciprocal_values) / answered if answered else 0.0,
        citation_hit_rate=citation_hits / answered if answered else 0.0,
        unauthorized_hits=unauthorized,
        case_count=len(cases),
        answered_case_count=answered,
        top_k=top_k,
    )
    logger.info("检索评测完成", recall=report.recall_at_k, mrr=report.mrr_at_k,
                citation_hit_rate=report.citation_hit_rate, unauthorized=unauthorized)
    return report
