"""危险 SQL 离线拦截率 evaluator。"""

from __future__ import annotations

from typing import Any


DANGEROUS_SQL_CASES: tuple[str, ...] = (
    "DROP TABLE orders",
    "DELETE FROM orders",
    "UPDATE orders SET amount = 0",
    "INSERT INTO orders (id, customer, amount) VALUES (9, 'x', 1)",
    "ALTER TABLE orders ADD COLUMN secret TEXT",
    "TRUNCATE TABLE orders",
    "CREATE TABLE copied AS SELECT * FROM orders",
    "SELECT * FROM orders; DROP TABLE orders",
    "SELECT pg_sleep(10)",
    "SELECT nextval('order_seq')",
    "SELECT * INTO orders_backup FROM orders",
    "SELECT * FROM orders FOR UPDATE",
)


# 方法作用：逐条运行危险 SQL 并统计只读安全边界的拦截率。
# Args: cases - 可选危险 SQL 序列；dialect - 校验方言。
# Returns: 总数、拦截数、拦截率和逐条结果。
def evaluate_security_cases(
    cases: tuple[str, ...] = DANGEROUS_SQL_CASES,
    *,
    dialect: str = "postgres",
) -> dict[str, Any]:
    from src.security.sql_execution import validate_sql

    details = []
    for sql in cases:
        result = validate_sql(sql, dialect)
        details.append({
            "sql": sql,
            "blocked": not result.valid,
            "errors": result.errors,
        })
    blocked = sum(1 for item in details if item["blocked"])
    total = len(details)
    return {
        "total": total,
        "blocked": blocked,
        "block_rate": blocked / total if total else 1.0,
        "details": details,
    }
