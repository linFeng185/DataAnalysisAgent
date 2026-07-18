"""知识证据轻量确定性重排器。"""

from __future__ import annotations

import re

from src.knowledge.asset_models import Evidence
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：融合向量、关键词、短语和来源多样性分数，对候选证据做确定性重排。
# Args: evidence - 初始召回证据；query - 用户查询；top_k - 返回数量。
# Returns: 按 rerank 分数降序排列的 Evidence 列表。
def rerank_evidence(evidence: list[Evidence], query: str, top_k: int = 5) -> list[Evidence]:
    logger.debug("知识证据重排入口", candidates=len(evidence), query=query[:80], top_k=top_k)
    if top_k <= 0:
        raise ValueError("top_k 必须大于零")
    query_lower = query.lower().strip()
    query_tokens = set(_tokenize(query_lower))
    scored: list[tuple[float, Evidence]] = []
    for item in evidence:
        vector_score = float(item.scores.get("vector", item.scores.get("relevance", 0.0)) or 0.0)
        lexical_score = float(item.scores.get("lexical", 0.0) or 0.0)
        content_lower = item.content.lower()
        field_text = " ".join(
            str(item.metadata.get(key, ""))
            for key in ("table_name", "column_name", "source_file", "tags")
        ).lower()
        phrase_score = 1.0 if query_lower and query_lower in content_lower else 0.0
        if not phrase_score and query_tokens:
            field_tokens = set(_tokenize(field_text))
            phrase_score = 1.0 if query_tokens and query_tokens <= field_tokens else 0.0
        score = vector_score * 0.5 + lexical_score * 0.35 + phrase_score * 0.15
        scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    selected: list[Evidence] = []
    source_counts: dict[str, int] = {}
    for base_score, item in scored:
        source = str(item.metadata.get("source_file", "") or item.source_id)
        duplicate_penalty = min(0.15, source_counts.get(source, 0) * 0.08)
        final_score = max(0.0, base_score - duplicate_penalty)
        item.scores["rerank"] = final_score
        item.metadata["rerank_method"] = "vector_lexical_phrase_diversity_v1"
        selected.append(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= top_k:
            break
    selected.sort(key=lambda item: item.scores.get("rerank", 0.0), reverse=True)
    logger.info("知识证据重排完成", selected=len(selected), sources=len(source_counts))
    return selected


# 方法作用：切分中英文专有名词和字段标识符。
# Args: text - 待分词文本。
# Returns: token 列表。
def _tokenize(text: str) -> list[str]:
    logger.debug("重排器分词入口", text_size=len(text))
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text.lower())
    logger.info("重排器分词完成", token_count=len(tokens))
    return tokens
