"""5.2 sqlglot 校验工具 — 三层语法校验 + LangChain Tool 封装。

功能：语法解析 / 函数白名单 / 方言转译
依据：SPEC §3.4.1 Layer 3 sqlglot 校验 Node
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp
from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)

# ── 5.2.2 支持的方言 (共 20+) ────────────────────────

SUPPORTED_DIALECTS: set[str] = {
    "clickhouse", "mysql", "postgres", "presto", "trino",
    "hive", "spark", "bigquery", "snowflake", "redshift",
    "duckdb", "sqlite", "tsql", "databricks", "teradata",
    "oracle", "starrocks", "doris", "tableau",
}

# ── 5.2.4 跨数据库通用函数 ────────────────────────────

_UNIVERSAL_FUNCTIONS: set[str] = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE",
    "CAST", "CASE", "NULLIF", "ROUND", "ABS",
    "UPPER", "LOWER", "LENGTH", "TRIM", "CONCAT",
    "NOW", "CURRENT_DATE", "CURRENT_TIMESTAMP",
}

# ── 5.2.5 方言函数映射表 ──────────────────────────────

_FUNCTION_MAPPINGS: dict[str, dict[str, str]] = {
    "clickhouse": {
        "DATE": "toDate()",
        "DATE_FORMAT": "formatDateTime()",
        "STR_TO_DATE": "parseDateTimeBestEffort()",
        "UNIX_TIMESTAMP": "toUnixTimestamp()",
        "GROUP_CONCAT": "groupArray()",
        "IFNULL": "ifNull()",
        "ROW_NUMBER": "row_number() | 需结合 WINDOW 子句",
    },
    "postgres": {
        "IFNULL": "COALESCE()",
        "DATE_FORMAT": "TO_CHAR()",
        "STR_TO_DATE": "TO_DATE()",
        "GROUP_CONCAT": "STRING_AGG()",
        "LIMIT n OFFSET m": "LIMIT n OFFSET m (PG 13+ 支持标准语法)",
    },
}


def _get_dialect_functions(dialect: str) -> set[str]:
    """5.2.3 获取指定方言的内置函数白名单。"""
    try:
        import importlib
        dialect_mod = importlib.import_module(f"sqlglot.dialects.{dialect}")
    except ModuleNotFoundError:
        logger.warning("sqlglot 方言模块不存在", dialect=dialect)
        return set()
    except Exception as exc:
        logger.error(
            "sqlglot 方言模块加载失败",
            dialect=dialect,
            error=str(exc),
            exc_info=True,
        )
        return set()
    try:
        for attr_name in dir(dialect_mod):
            if attr_name.lower() == dialect.lower():
                dialect_cls = getattr(dialect_mod, attr_name)
                if hasattr(dialect_cls, "generator_class"):
                    generator = dialect_cls.generator_class()
                    funcs: set[str] = set()
                    funcs.update(k.lower() for k in vars(generator.TRANSFORMS))
                    funcs.update(k.lower() for k in vars(exp.Func))
                    return funcs
        return set()
    except Exception as exc:
        logger.error(
            "sqlglot 方言函数读取失败",
            dialect=dialect,
            error=str(exc),
            exc_info=True,
        )
        return set()


def _is_universal_func(name: str) -> bool:
    """5.2.4 判断是否为跨数据库通用函数，跳过方言检查。"""
    return name.upper() in _UNIVERSAL_FUNCTIONS


def _suggest_correct_function(func_name: str, dialect: str) -> str | None:
    """5.2.5 根据方言提供函数修正建议。"""
    dialect_mapping = _FUNCTION_MAPPINGS.get(dialect, {})
    return dialect_mapping.get(func_name.upper())


def validate_with_sqlglot(sql: str, dialect: str) -> dict:
    """5.2.1 sqlglot 三层校验。

    1. 语法解析 → 拦截语法错误
    2. 函数名白名单 → 拦截 LLM 幻觉函数
    3. 方言转译 → 可选，将标准 SQL 转为目标方言

    返回: {"valid": bool, "errors": list, "warnings": list, "transpiled_sql": str}
    """
    result: dict[str, Any] = {
        "valid": True, "errors": [], "warnings": [], "transpiled_sql": sql,
    }

    # ---- 1. 语法解析 ----
    try:
        parsed = sqlglot.parse(sql, dialect=dialect)
        if not parsed or not parsed[0]:
            result["valid"] = False
            result["errors"].append("无法解析: SQL 解析返回空")
            return result
    except sqlglot.errors.ParseError as e:
        result["valid"] = False
        result["errors"].append({
            "type": "syntax_error",
            "message": str(e),
            "line": e.errors[0].get("line") if hasattr(e, "errors") and e.errors else None,
            "suggestion": "请检查关键字拼写、括号匹配、引号配对",
        })
        return result

    # ---- 2. 函数白名单校验 ----
    dialect_funcs = _get_dialect_functions(dialect)
    for node in parsed[0].walk():
        if isinstance(node, (exp.Anonymous, exp.Func)):
            func_name = (node.sql_name() or "").upper()
            if func_name and not _is_universal_func(func_name) and func_name.lower() not in dialect_funcs:
                suggestion = _suggest_correct_function(func_name, dialect)
                result["warnings"].append({
                    "type": "unknown_function",
                    "function": func_name,
                    "suggestion": suggestion or f"'{func_name}' 在 {dialect} 中不存在，请替换为等价函数",
                })

    # ---- 3. 方言转译 ----
    if dialect != "mysql":
        try:
            result["transpiled_sql"] = sqlglot.transpile(
                sql, read="mysql", write=dialect
            )[0]
        except Exception as exc:
            logger.warning(
                "SQL 方言转译失败，保留原始校验结果",
                dialect=dialect,
                error=str(exc),
                exc_info=True,
            )

    return result


# ── 5.1.3 LangChain Tool 封装 ──────────────────────────

class SQLglotValidatorTool(BaseTool):
    """SQL 语法校验工具 — 封装 validate_with_sqlglot()。

    用于 Agent 场景，LLM 可自行调用此工具校验生成的 SQL。
    """

    name: str = "sqlglot_validator"
    description: str = (
        "校验 SQL 语句的语法正确性和方言兼容性。"
        "输入: JSON 格式 {\"sql\": \"SELECT ...\", \"dialect\": \"mysql\"}。"
        "返回: 校验结果，包含 valid/errors/warnings/transpiled_sql。"
    )
    dialect: str = "mysql"

    def _run(
        self,
        sql: str,
        dialect: str = "",
        run_manager: Any = None,
    ) -> dict:
        dialect = dialect or self.dialect
        logger.info("sqlglot 校验工具调用", dialect=dialect, sql=sql[:120])
        return validate_with_sqlglot(sql, dialect)
