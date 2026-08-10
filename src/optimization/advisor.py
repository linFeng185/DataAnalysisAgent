"""基于 sqlglot 的只读 SQL 优化建议器。"""

from __future__ import annotations

import json
import math
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from src.logging_config import get_logger
from src.security.sql_execution import normalize_sql_dialect, validate_sql

logger = get_logger(__name__)
_MAX_SQL_CHARS = 20_000
_MAX_METADATA_NODES = 5_000
_MAX_METADATA_DEPTH = 8
_MAX_METADATA_STRING_CHARS = 20_000


class OptimizationIssue(BaseModel):
    """单条可审计优化问题。"""

    code: str
    severity: Literal["low", "medium", "high"]
    category: Literal["index", "rewrite", "scan", "aggregation", "sargability"]
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    recommendation: str


class CandidateIndex(BaseModel):
    """只描述候选索引，不包含自动执行动作。"""

    table: str
    columns: list[str]
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class OptimizationReport(BaseModel):
    """自动优化建议的稳定输出契约。"""

    valid: bool
    dialect: str
    normalized_sql: str = ""
    candidate_sql: str = ""
    issues: list[OptimizationIssue] = Field(default_factory=list)
    candidate_indexes: list[CandidateIndex] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    evidence_sources: list[str] = Field(default_factory=list)
    evidence_quality: Literal["low", "medium", "high"] = "low"
    limitations: list[str] = Field(default_factory=list)
    executed: bool = False


class OptimizationAdvisor:
    """对只读 SQL 进行不访问数据库的静态优化分析。"""

    # 方法作用：分析 SQL、索引和表规模并生成不执行任何 DDL 的优化报告。
    # Args: self - 当前建议器；sql/dialect - SQL 与方言；schema/indexes/statistics - 可选元数据；execution_ms - 可选耗时。
    # Returns: 通过统一契约表达的静态优化建议。
    def advise(
        self,
        sql: str,
        dialect: str,
        *,
        schema: dict[str, Any] | None = None,
        indexes: dict[str, Any] | None = None,
        statistics: dict[str, Any] | None = None,
        execution_ms: float | None = None,
    ) -> OptimizationReport:
        normalized_dialect = normalize_sql_dialect(dialect)
        logger.debug(
            "SQL 优化建议入口",
            dialect=normalized_dialect,
            sql_chars=len(sql or ""),
            has_schema=bool(schema),
            has_indexes=bool(indexes),
        )
        budget_error = _input_budget_error(sql, schema, indexes, statistics, execution_ms)
        if budget_error:
            logger.warning("SQL 优化建议资源预算拒绝", reason=budget_error)
            return OptimizationReport(
                valid=False,
                dialect=normalized_dialect,
                issues=[OptimizationIssue(
                    code="RESOURCE_BUDGET_EXCEEDED",
                    severity="high",
                    category="rewrite",
                    message=budget_error,
                    evidence={"source": "input_budget"},
                    recommendation="缩小 SQL 或元数据范围后重试",
                )],
                limitations=["超出资源预算时不解析 SQL、不生成候选索引"],
            )
        validation = validate_sql(sql, normalized_dialect)
        if not validation.valid:
            messages = [str(item.get("message", "SQL 校验失败")) for item in validation.errors]
            logger.warning(
                "SQL 优化建议拒绝",
                dialect=normalized_dialect,
                error_count=len(messages),
            )
            return OptimizationReport(
                valid=False,
                dialect=normalized_dialect,
                issues=[OptimizationIssue(
                    code="INVALID_SQL",
                    severity="high",
                    category="rewrite",
                    message="；".join(messages) or "SQL 校验失败",
                    recommendation="先修正 SQL 并通过平台只读安全校验",
                )],
                limitations=["无效或非只读 SQL 不生成优化候选"],
            )
        try:
            tree = sqlglot.parse_one(validation.sql, read=normalized_dialect or None)
        except sqlglot.errors.ParseError as exc:
            logger.warning("SQL 优化解析拒绝", dialect=normalized_dialect, error=str(exc))
            return OptimizationReport(
                valid=False,
                dialect=normalized_dialect,
                issues=[OptimizationIssue(
                    code="PARSE_FAILED",
                    severity="high",
                    category="rewrite",
                    message="SQL 无法解析",
                    evidence={"error": str(exc)[:300]},
                    recommendation="确认 SQL 与声明方言一致",
                )],
                limitations=["解析失败时不生成候选改写"],
            )

        normalized_sql = tree.sql(dialect=normalized_dialect or None, pretty=False)
        table_names = sorted({table.name for table in tree.find_all(exp.Table) if table.name})
        issues: list[OptimizationIssue] = []
        candidate_indexes: list[CandidateIndex] = []
        known_schema = _normalize_schema(schema or {})
        indexed = _normalize_indexes(indexes or {})

        if any(True for _ in tree.find_all(exp.Star)):
            issues.append(OptimizationIssue(
                code="SELECT_STAR",
                severity="medium",
                category="scan",
                message="SELECT * 可能读取不需要的列并增加网络与序列化开销",
                evidence={"source": "sql_ast", "tables": table_names},
                recommendation="显式列出业务需要的字段",
            ))

        if not any(True for _ in tree.find_all(exp.Limit)):
            issues.append(OptimizationIssue(
                code="NO_LIMIT",
                severity="low",
                category="scan",
                message="查询没有显式结果上限",
                evidence={"source": "sql_ast"},
                recommendation="交互式明细查询增加合理 LIMIT，聚合查询按业务需要保留",
            ))

        non_sargable = _non_sargable_columns(tree)
        if non_sargable:
            issues.append(OptimizationIssue(
                code="NON_SARGABLE",
                severity="medium",
                category="sargability",
                message="过滤列被函数包裹，普通索引可能无法生效",
                evidence={"source": "sql_ast", "columns": non_sargable},
                recommendation="优先改写为范围条件，必要时评估函数索引或生成列",
            ))

        predicate_columns = _predicate_columns(tree, table_names)
        for table, columns in sorted(predicate_columns.items()):
            missing = [column for column in columns if not _is_indexed(indexed, table, column)]
            if not missing:
                continue
            known_columns = known_schema.get(table.lower(), set())
            if not known_columns:
                issues.append(OptimizationIssue(
                    code="INDEX_EVIDENCE_INCOMPLETE",
                    severity="low",
                    category="index",
                    message=f"表 {table} 缺少可验证 Schema，未生成候选索引",
                    evidence={"source": "schema", "columns": missing[:10]},
                    recommendation="提供当前数据源 Schema 与完整索引元数据后重新分析",
                ))
                continue
            unknown = [column for column in missing if column.lower() not in known_columns]
            if unknown:
                issues.append(OptimizationIssue(
                    code="UNVERIFIED_PREDICATE_COLUMN",
                    severity="high",
                    category="index",
                    message=f"表 {table} 的条件列无法由 Schema 验证",
                    evidence={"source": "schema", "columns": unknown[:10]},
                    recommendation="确认 SQL 列名、别名和 Schema 版本后重新分析",
                ))
            verified_missing = [column for column in missing if column.lower() in known_columns]
            if not verified_missing:
                continue
            candidate = CandidateIndex(
                table=table,
                columns=verified_missing[:3],
                reason="WHERE/JOIN 条件列未在已提供索引的首列中",
                evidence={
                    "source": "schema+indexes+sql_ast",
                    "predicate_columns": verified_missing[:3],
                },
            )
            candidate_indexes.append(candidate)
            issues.append(OptimizationIssue(
                code="MISSING_INDEX",
                severity="medium",
                category="index",
                message=f"表 {table} 的过滤或连接列缺少可见索引",
                evidence={"source": "schema+indexes+sql_ast", "columns": candidate.columns},
                recommendation="结合选择性、写入成本和 EXPLAIN 验证候选复合索引",
            ))

        large_tables = _large_tables(statistics or {}, table_names)
        has_group = any(True for _ in tree.find_all(exp.Group))
        if has_group and large_tables:
            issues.append(OptimizationIssue(
                code="LARGE_AGGREGATION",
                severity="medium",
                category="aggregation",
                message="大表聚合可能触发高成本扫描和排序",
                evidence={"source": "statistics+sql_ast", "tables": large_tables},
                recommendation="确认分区裁剪，评估预聚合表或物化视图",
            ))
        if has_group and execution_ms is not None and execution_ms >= 3000:
            issues.append(OptimizationIssue(
                code="MATERIALIZED_VIEW_CANDIDATE",
                severity="low",
                category="aggregation",
                message="重复慢聚合可能适合预计算",
                evidence={"source": "execution_observation", "execution_ms": execution_ms},
                recommendation="在更新时效、存储和刷新成本评审后考虑物化视图",
            ))

        limitations = ["建议基于静态证据，应用前必须使用真实 EXPLAIN 验证"]
        if not indexed:
            limitations.append("未提供完整索引元数据，索引建议可能与现有索引重复")
        if table_names and not known_schema:
            limitations.append("未提供 Schema，无法验证候选列类型和基数")

        evidence_sources = ["sql_ast"]
        if known_schema:
            evidence_sources.append("schema")
        if indexed:
            evidence_sources.append("indexes")
        if statistics:
            evidence_sources.append("statistics")
        if execution_ms is not None and math.isfinite(execution_ms):
            evidence_sources.append("execution_observation")
        evidence_quality: Literal["low", "medium", "high"] = "low"
        if known_schema and indexed:
            evidence_quality = "medium"
        if known_schema and indexed and statistics and execution_ms is not None:
            evidence_quality = "high"
        report = OptimizationReport(
            valid=True,
            dialect=normalized_dialect,
            normalized_sql=normalized_sql,
            candidate_sql=normalized_sql,
            issues=_stable_issues(issues),
            candidate_indexes=_stable_indexes(candidate_indexes),
            tables=table_names,
            evidence_sources=evidence_sources,
            evidence_quality=evidence_quality,
            limitations=limitations,
            executed=False,
        )
        logger.info(
            "SQL 优化建议完成",
            dialect=normalized_dialect,
            issue_count=len(report.issues),
            index_count=len(report.candidate_indexes),
            executed=False,
        )
        return report


# 方法作用：从 WHERE 和 JOIN 条件中提取可能需要索引的列。
# Args: tree - SQL AST；table_names - 查询表名。
# Returns: 表名到去重列名列表的映射。
def _predicate_columns(tree: exp.Expression, table_names: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    scopes: list[exp.Expression] = list(tree.find_all(exp.Where)) + list(tree.find_all(exp.Join))
    fallback_table = table_names[0] if len(table_names) == 1 else ""
    aliases = {table.alias_or_name: table.name for table in tree.find_all(exp.Table)}
    for scope in scopes:
        for column in scope.find_all(exp.Column):
            table = aliases.get(column.table, column.table) or fallback_table
            if not table or not column.name:
                continue
            result.setdefault(table, [])
            if column.name not in result[table]:
                result[table].append(column.name)
    return result


# 方法作用：找出过滤条件中被函数包裹的列引用。
# Args: tree - SQL AST。
# Returns: 排序去重后的列引用。
def _non_sargable_columns(tree: exp.Expression) -> list[str]:
    columns: set[str] = set()
    for where in tree.find_all(exp.Where):
        for function in where.find_all(exp.Func):
            for column in function.find_all(exp.Column):
                columns.add(column.sql())
    return sorted(columns)


# 方法作用：把不同 API 形态的索引元数据归一为表和索引列序列。
# Args: indexes - 原始索引映射。
# Returns: 表名到索引列元组列表的映射。
def _normalize_indexes(indexes: dict[str, Any]) -> dict[str, list[tuple[str, ...]]]:
    normalized: dict[str, list[tuple[str, ...]]] = {}
    for table, raw_items in indexes.items():
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        for item in items:
            columns = item.get("columns", []) if isinstance(item, dict) else item
            if isinstance(columns, str):
                columns = [columns]
            values = tuple(str(column).strip().lower() for column in columns if str(column).strip())
            if values:
                normalized.setdefault(str(table).lower(), []).append(values)
    return normalized


# 方法作用：判断某列是否为已有单列或复合索引的首列。
# Args: indexes - 归一化索引；table/column - 目标表列。
# Returns: 能使用已知索引时返回 True。
def _is_indexed(indexes: dict[str, list[tuple[str, ...]]], table: str, column: str) -> bool:
    return any(
        columns and columns[0] == column.lower()
        for columns in indexes.get(table.lower(), [])
    )


# 方法作用：归一化 Schema 映射以判断调用方是否提供元数据。
# Args: schema - 原始 Schema。
# Returns: 表名到列名集合的映射。
def _normalize_schema(schema: dict[str, Any]) -> dict[str, set[str]]:
    normalized: dict[str, set[str]] = {}
    raw_tables = schema.get("tables") if isinstance(schema.get("tables"), list) else None
    entries = raw_tables or [{"name": key, **(value if isinstance(value, dict) else {})}
                             for key, value in schema.items()]
    for item in entries:
        if not isinstance(item, dict):
            continue
        table = str(item.get("name", "")).strip()
        raw_columns = item.get("columns", [])
        columns = {
            str(column.get("name", "") if isinstance(column, dict) else column).strip()
            for column in raw_columns
        }
        if table:
            normalized[table.lower()] = {column.lower() for column in columns if column}
    return normalized


# 方法作用：从表统计中筛出达到百万行阈值的大表。
# Args: statistics - 表统计；table_names - 当前查询表。
# Returns: 表名到行数的映射。
def _large_tables(statistics: dict[str, Any], table_names: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in table_names:
        raw = statistics.get(table, {})
        row_count = raw.get("row_count", 0) if isinstance(raw, dict) else raw
        try:
            count = int(row_count or 0)
        except (TypeError, ValueError):
            count = 0
        if count >= 1_000_000:
            result[table] = count
    return result


# 方法作用：按问题代码和证据去除重复建议。
# Args: issues - 原始建议列表。
# Returns: 保留首次出现顺序的建议列表。
def _deduplicate_issues(issues: list[OptimizationIssue]) -> list[OptimizationIssue]:
    seen: set[tuple[str, str]] = set()
    result: list[OptimizationIssue] = []
    for issue in issues:
        key = (issue.code, str(issue.evidence))
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


# 方法作用：去重后按严重级别、类别、代码和证据稳定排序建议。
# Args: issues - 原始建议列表。
# Returns: 跨进程顺序稳定的建议列表。
def _stable_issues(issues: list[OptimizationIssue]) -> list[OptimizationIssue]:
    severity_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        _deduplicate_issues(issues),
        key=lambda item: (
            severity_order[item.severity],
            item.category,
            item.code,
            json.dumps(item.evidence, ensure_ascii=True, sort_keys=True, default=str),
        ),
    )


# 方法作用：去重并稳定排序候选索引，防止相同证据产生重复建议。
# Args: indexes - 原始候选索引列表。
# Returns: 按表名和列顺序稳定排列的候选索引。
def _stable_indexes(indexes: list[CandidateIndex]) -> list[CandidateIndex]:
    unique: dict[tuple[str, tuple[str, ...]], CandidateIndex] = {}
    for item in indexes:
        key = (item.table.lower(), tuple(column.lower() for column in item.columns))
        unique.setdefault(key, item)
    return [unique[key] for key in sorted(unique)]


# 方法作用：在 SQL 解析前验证输入大小、嵌套深度和数值证据预算。
# Args: sql - SQL 文本；schema/indexes/statistics - 调用方元数据；execution_ms - 可选执行耗时。
# Returns: 超限原因；输入可接受时返回空字符串。
def _input_budget_error(
    sql: str,
    schema: dict[str, Any] | None,
    indexes: dict[str, Any] | None,
    statistics: dict[str, Any] | None,
    execution_ms: float | None,
) -> str:
    if len(str(sql or "")) > _MAX_SQL_CHARS:
        return f"SQL 长度不能超过 {_MAX_SQL_CHARS} 字符"
    if execution_ms is not None and not math.isfinite(float(execution_ms)):
        return "execution_ms 必须是有限值"
    node_count = 0
    stack: list[tuple[Any, int]] = [
        (schema or {}, 1),
        (indexes or {}, 1),
        (statistics or {}, 1),
    ]
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_METADATA_NODES:
            return f"Schema、索引和统计元数据合计不能超过 {_MAX_METADATA_NODES} 个节点"
        if depth > _MAX_METADATA_DEPTH:
            return f"元数据嵌套深度不能超过 {_MAX_METADATA_DEPTH} 层"
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
            stack.extend((key, depth + 1) for key in value)
        elif isinstance(value, (list, tuple, set)):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and len(value) > _MAX_METADATA_STRING_CHARS:
            return f"单个元数据文本不能超过 {_MAX_METADATA_STRING_CHARS} 字符"
        elif isinstance(value, float) and not math.isfinite(value):
            return "元数据数值必须是有限值"
    return ""
