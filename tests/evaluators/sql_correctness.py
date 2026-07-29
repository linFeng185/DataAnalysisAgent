"""基于 sqlglot AST 的 SQL 正确性 evaluator。"""

from __future__ import annotations

from typing import Any


# 方法作用：解析 SQL 并生成稳定的规范化表示。
# Args: sql - 待规范化 SQL；dialect - sqlglot 方言。
# Returns: 规范化 SQL 文本。
def normalize_sql(sql: str, dialect: str = "") -> str:
    import sqlglot

    tree = sqlglot.parse_one(sql, read=dialect or None)
    if tree is None:
        raise ValueError("SQL 解析结果为空")
    return tree.sql(dialect=dialect or None, pretty=False, normalize=True)


# 方法作用：比较生成 SQL 与标注 SQL 的 AST 结构并返回评测明细。
# Args: actual_sql - 模型生成 SQL；expected_sql - 标注 SQL；dialect - 方言。
# Returns: 包含 score、normalized_actual、normalized_expected 和 error 的字典。
def evaluate_sql_correctness(
    actual_sql: str,
    expected_sql: str,
    *,
    dialect: str = "",
) -> dict[str, Any]:
    try:
        normalized_actual = normalize_sql(actual_sql, dialect)
        normalized_expected = normalize_sql(expected_sql, dialect)
    except Exception as exc:
        return {
            "score": 0.0,
            "normalized_actual": "",
            "normalized_expected": "",
            "error": str(exc),
        }
    return {
        "score": 1.0 if normalized_actual == normalized_expected else 0.0,
        "normalized_actual": normalized_actual,
        "normalized_expected": normalized_expected,
        "error": "",
    }
