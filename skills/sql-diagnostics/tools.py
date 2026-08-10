"""只读 SQL 静态诊断工具。"""

from __future__ import annotations

from typing import Any

import sqlglot
from langchain_core.tools import BaseTool
from sqlglot import exp

from src.logging_config import get_logger
from src.tools.sql_rewriter import rewrite_sql
from src.tools.sqlglot_validator import validate_with_sqlglot

logger = get_logger(__name__)
_DIALECTS = {"clickhouse", "mysql", "postgres", "oracle", "mssql", "sqlite"}


class DiagnoseSqlTool(BaseTool):
    """校验、规范化并静态分析 SQL，不连接数据库。"""

    name: str = "diagnose_sql"
    description: str = (
        "执行只读安全校验、方言兼容性检查、规范化改写和静态复杂度诊断，绝不执行 SQL。"
    )

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("diagnose_sql 只允许异步执行")

    async def _arun(
        self,
        sql: str,
        dialect: str,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回 SQL 的安全、兼容性和复杂度诊断。"""
        del run_manager
        normalized_dialect = dialect.strip().lower()
        logger.info("SQL 诊断开始", dialect=normalized_dialect, sql_chars=len(sql))
        if not sql.strip():
            raise ValueError("sql 不能为空")
        if normalized_dialect not in _DIALECTS:
            raise ValueError(f"不支持的数据库方言: {normalized_dialect}")

        validation = validate_with_sqlglot(sql, normalized_dialect)
        tree = None
        parse_error = ""
        try:
            tree = sqlglot.parse_one(sql, read=_parse_dialect(normalized_dialect))
        except sqlglot.errors.ParseError as exc:
            parse_error = str(exc)

        rewritten_sql = ""
        if validation.get("valid"):
            rewritten_sql = rewrite_sql(sql, normalized_dialect)
        complexity = _complexity(tree)
        risks = _risk_flags(tree, complexity)
        errors = list(validation.get("errors", []) or [])
        if parse_error and parse_error not in errors:
            errors.append(parse_error)
        result = {
            "valid": bool(validation.get("valid")) and not parse_error,
            "dialect": normalized_dialect,
            "errors": errors,
            "warnings": list(validation.get("warnings", []) or []),
            "rewritten_sql": rewritten_sql,
            "tables": sorted({table.name for table in tree.find_all(exp.Table)}) if tree else [],
            "columns": sorted({column.sql() for column in tree.find_all(exp.Column)})
            if tree
            else [],
            "complexity": complexity,
            "risk_flags": risks,
            "executed": False,
        }
        logger.info(
            "SQL 诊断完成",
            valid=result["valid"],
            error_count=len(errors),
            warning_count=len(result["warnings"]),
            risk_count=len(risks),
        )
        return result


def _parse_dialect(dialect: str) -> str:
    """把项目方言名映射为 sqlglot 解析方言。"""
    return "tsql" if dialect == "mssql" else dialect


def _complexity(tree: exp.Expression | None) -> dict[str, int]:
    """统计主要 SQL 结构数量。"""
    if tree is None:
        return {"joins": 0, "subqueries": 0, "unions": 0, "windows": 0, "ctes": 0, "wildcards": 0}
    return {
        "joins": sum(1 for _ in tree.find_all(exp.Join)),
        "subqueries": sum(1 for _ in tree.find_all(exp.Subquery)),
        "unions": sum(1 for _ in tree.find_all(exp.Union)),
        "windows": sum(1 for _ in tree.find_all(exp.Window)),
        "ctes": sum(1 for _ in tree.find_all(exp.CTE)),
        "wildcards": sum(1 for _ in tree.find_all(exp.Star)),
    }


def _risk_flags(tree: exp.Expression | None, complexity: dict[str, int]) -> list[dict[str, str]]:
    """生成不依赖执行计划的静态风险提示。"""
    if tree is None:
        return [{"code": "PARSE_FAILED", "severity": "high", "message": "SQL 无法解析"}]
    flags: list[dict[str, str]] = []
    if complexity["wildcards"]:
        flags.append(
            {"code": "SELECT_STAR", "severity": "medium", "message": "使用通配符可能读取不必要的列"}
        )
    if complexity["joins"] >= 5:
        flags.append(
            {
                "code": "MANY_JOINS",
                "severity": "medium",
                "message": "连接数量较多，需要结合 EXPLAIN 检查基数和索引",
            }
        )
    if complexity["subqueries"] >= 3:
        flags.append(
            {
                "code": "NESTED_SUBQUERIES",
                "severity": "medium",
                "message": "子查询较多，建议检查是否可改写为公共表表达式",
            }
        )
    if not any(True for _ in tree.find_all(exp.Limit)):
        flags.append(
            {
                "code": "NO_LIMIT",
                "severity": "low",
                "message": "查询未声明 LIMIT，实际执行仍应由平台行数上限保护",
            }
        )
    return flags


_TOOLS: dict[str, BaseTool] = {"diagnose_sql": DiagnoseSqlTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
