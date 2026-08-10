"""数据库模式词法映射工具。"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class LinkSchemaTool(BaseTool):
    """将用户术语映射为可解释的表和字段候选。"""

    name: str = "link_schema"
    description: str = "根据名称、别名和描述生成表字段候选、置信分数、歧义和关联键建议。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("link_schema 只允许异步执行")

    async def _arun(
        self,
        query: str,
        tables: list[dict[str, Any]],
        threshold: float = 0.35,
        max_matches: int = 20,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """执行不依赖模型的词法匹配并保留评分原因。"""
        del run_manager
        normalized_query = _normalize(query)
        logger.info(
            "模式映射开始",
            query_chars=len(query),
            table_count=len(tables),
            threshold=threshold,
        )
        if not normalized_query:
            raise ValueError("query 不能为空")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold 必须在 0 到 1 之间")
        if not 1 <= max_matches <= 100:
            raise ValueError("max_matches 必须在 1 到 100 之间")
        _validate_tables(tables)

        matches: list[dict[str, Any]] = []
        table_context: dict[str, float] = {}
        for table in tables:
            table_name = str(table["name"])
            table_labels = _labels(table)
            table_score, table_term, table_reasons = _best_score(query, table_labels)
            table_context[table_name] = table_score
            if table_score >= threshold:
                matches.append(
                    {
                        "object_kind": "table",
                        "table": table_name,
                        "column": "",
                        "data_type": "",
                        "matched_term": table_term,
                        "score": table_score,
                        "reasons": table_reasons,
                    }
                )
            for column in table.get("columns", []):
                score, term, reasons = _best_score(query, _labels(column))
                if table_score >= threshold:
                    score = min(1.0, round(score + 0.05, 4))
                    reasons = [*reasons, "所属表同时命中"]
                if score < threshold:
                    continue
                matches.append(
                    {
                        "object_kind": "column",
                        "table": table_name,
                        "column": str(column["name"]),
                        "data_type": str(column.get("data_type", "")),
                        "matched_term": term,
                        "score": score,
                        "reasons": reasons,
                    }
                )

        matches.sort(key=lambda item: (-item["score"], item["table"], item["column"]))
        matches = matches[:max_matches]
        ambiguities = _find_ambiguities(matches)
        join_candidates = _find_join_candidates(tables)
        result = {
            "query": query,
            "matches": matches,
            "ambiguities": ambiguities,
            "join_candidates": join_candidates,
            "unmapped": not matches,
            "method": "名称、别名和描述的确定性词法匹配，不代表业务语义已确认",
        }
        logger.info(
            "模式映射完成",
            match_count=len(matches),
            ambiguity_count=len(ambiguities),
            join_candidate_count=len(join_candidates),
        )
        return result


def _validate_tables(tables: list[dict[str, Any]]) -> None:
    """校验表和字段定义的最小结构。"""
    seen_tables: set[str] = set()
    for table in tables:
        if not isinstance(table, dict) or not str(table.get("name", "")).strip():
            raise ValueError("每个表都必须提供非空 name")
        table_name = str(table["name"])
        if table_name in seen_tables:
            raise ValueError(f"表名重复: {table_name}")
        seen_tables.add(table_name)
        columns = table.get("columns", [])
        if not isinstance(columns, list):
            raise ValueError(f"表 {table_name} 的 columns 必须是数组")
        seen_columns: set[str] = set()
        for column in columns:
            if not isinstance(column, dict) or not str(column.get("name", "")).strip():
                raise ValueError(f"表 {table_name} 包含无效字段")
            column_name = str(column["name"])
            if column_name in seen_columns:
                raise ValueError(f"表 {table_name} 的字段名重复: {column_name}")
            seen_columns.add(column_name)


def _labels(item: dict[str, Any]) -> list[tuple[str, str]]:
    """收集名称、别名和描述及其来源。"""
    result = [(str(item.get("name", "")), "名称")]
    result.extend((str(alias), "别名") for alias in item.get("aliases", []) if str(alias).strip())
    if str(item.get("description", "")).strip():
        result.append((str(item["description"]), "描述"))
    return result


def _best_score(query: str, labels: list[tuple[str, str]]) -> tuple[float, str, list[str]]:
    """返回所有标签中的最高词法分数及解释。"""
    query_normalized = _normalize(query)
    query_tokens = _tokens(query)
    best = (0.0, "", ["未发现词法重合"])
    for label, source in labels:
        label_normalized = _normalize(label)
        if not label_normalized:
            continue
        reasons: list[str] = []
        if label_normalized in query_normalized:
            score = 0.98 if source in {"名称", "别名"} else 0.88
            reasons.append(f"{source}完整命中")
        else:
            label_tokens = _tokens(label)
            union = query_tokens | label_tokens
            overlap = len(query_tokens & label_tokens) / len(union) if union else 0.0
            similarity = SequenceMatcher(None, label_normalized, query_normalized).ratio()
            score = min(0.85, overlap * 0.7 + similarity * 0.3)
            if overlap:
                reasons.append(f"{source}词元重合")
            if similarity >= 0.5:
                reasons.append(f"{source}文本相似")
        candidate = (round(score, 4), label, reasons or ["弱词法相似"])
        if candidate[0] > best[0]:
            best = candidate
    return best


def _normalize(value: str) -> str:
    """执行兼容中文和英文标识符的文本规范化。"""
    normalized = unicodedata.normalize("NFKC", str(value)).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _tokens(value: str) -> set[str]:
    """生成英文词元、中文片段及中文双字片段。"""
    normalized = unicodedata.normalize("NFKC", str(value)).lower()
    parts = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized)
    result: set[str] = set(parts)
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", part) and len(part) > 1:
            result.update(part[index : index + 2] for index in range(len(part) - 1))
    return result


def _find_ambiguities(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """标记最高候选分数接近且对象不同的歧义。"""
    if len(matches) < 2:
        return []
    top_score = matches[0]["score"]
    candidates = [item for item in matches if top_score - item["score"] <= 0.05]
    if len(candidates) < 2:
        return []
    return [
        {
            "reason": "多个候选分数差不超过 0.05，需要结合真实 Schema 或用户确认",
            "candidates": [
                {"table": item["table"], "column": item["column"], "score": item["score"]}
                for item in candidates
            ],
        }
    ]


def _find_join_candidates(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仅按规范化同名字段生成关联键候选，不自动执行连接。"""
    by_name: dict[str, list[tuple[str, str]]] = {}
    for table in tables:
        for column in table.get("columns", []):
            normalized = _normalize(str(column["name"]))
            if normalized:
                by_name.setdefault(normalized, []).append((str(table["name"]), str(column["name"])))
    result: list[dict[str, Any]] = []
    for occurrences in by_name.values():
        for left_index, left in enumerate(occurrences):
            for right in occurrences[left_index + 1 :]:
                if left[0] == right[0]:
                    continue
                result.append(
                    {
                        "left_table": left[0],
                        "left_column": left[1],
                        "right_table": right[0],
                        "right_column": right[1],
                        "confidence": "medium",
                        "reason": "字段规范化后同名，仍需 JoinContract 验证基数和匹配率",
                    }
                )
    return result


_TOOLS: dict[str, BaseTool] = {"link_schema": LinkSchemaTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
