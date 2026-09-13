"""4.5 layer3_validate Node — sqlglot 语法校验 + 安全拦截。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)

# 方法作用：兼容旧调用并委托统一 SQL 安全校验服务。
# Args: sql - 待验证 SQL；dialect - 数据源真实方言。
# Returns: 错误列表，空列表表示只读校验通过。
def validate_readonly_sql(sql: str, dialect: str) -> list[dict]:
    """使用正则与 sqlglot AST 验证 SQL 仅包含只读语句。

    Args:
        sql: 待验证的 SQL 文本。
        dialect: sqlglot 使用的数据库方言。

    Returns:
        错误列表；空列表表示校验通过。
    """
    logger.debug("只读 SQL 兼容校验入口", dialect=dialect, sql_chars=len(sql))
    from src.security.sql_execution import validate_sql

    result = validate_sql(sql, dialect)
    logger.info("只读 SQL 兼容校验完成", dialect=dialect, valid=result.valid)
    return result.errors


# 方法作用：在 SQL 生成后、EXPLAIN 前执行只读安全校验与两阶段列引用校验。
# Args: state - 当前 LangGraph 分析状态，含 generated_sql 与 relevant_tables。
# Returns: 状态增量，含 sql_valid、validation_errors、transpiled_sql 与执行上下文。
async def layer3_validate_node(state: AnalysisState) -> dict:
    """安全拦截 + sqlglot 语法校验。"""
    from src.graph.context import read_contexts, resolve_request_identity

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.debug("节点开始", node="layer3_validate")
    sql = state.get("generated_sql", "").strip()

    errors = validate_readonly_sql(sql, contexts.execution.dialect or "clickhouse")

    # 列引用校验必须早于 layer4_explain：EXPLAIN 会直接把 SQL 打到数据库，
    # 幻觉列在那里只能拿到原始 DB 报错，模型看不到可用列清单，重试只能继续猜。
    if not errors:
        from src.security.sql_schema_validation import validate_sql_schema

        datasource = contexts.routing.datasource or ""
        dialect_value = contexts.execution.dialect or ""
        # 阶段一基准：同一变量同时供校验入参与阶段二触发判断，避免两处表达式日后漂移
        tables_hint = state.get("relevant_tables", []) or []
        issues = validate_sql_schema(
            sql,
            datasources=[datasource] if datasource else [],
            dialect=dialect_value,
            tables=tables_hint,
        )
        high_issues = [item for item in issues if item.confidence == "high"]

        # 阶段二触发条件：有 high 置信问题，或阶段一根本没有基准可比对。
        # relevant_tables 为空时索引为空，「表不存在」只是无法判定的推断，
        # 必须用权威快照复核一次；这正是设计 §6 修掉「空 tables 静默跳过」空窗的方式。
        need_recheck = bool(high_issues) or not tables_hint
        if need_recheck and datasource:
            # 阶段二取数必须带上显式租户：与 retrieve_schema 的调用约定一致，
            # 不带租户会落到平台级回落，多租户下可能读到别家数据源的 Schema 缓存。
            tenant_id, _ = resolve_request_identity(state)
            confirmed = await _recheck_with_full_snapshot(
                sql, datasource, dialect_value, high_issues, tenant_id, tables_hint
            )
            if confirmed is not None:
                high_issues = confirmed

        if high_issues:
            message = "; ".join(item.describe() for item in high_issues[:3])
            errors = [
                {
                    "type": "column_reference",
                    "message": message,
                    "issues": [
                        {
                            "type": item.type,
                            "message": item.describe(),
                            "datasource": item.datasource,
                            "table": item.table,
                            "column": item.column,
                            "qualifier": item.qualifier,
                            "provided_by": list(item.provided_by),
                            "available_columns": list(item.available_columns),
                            # 真实列总数随 issue 下传，回喂文案报「共 N 个」时必须用它，
                            # 不能拿被 MAX_AVAILABLE_COLUMNS 截断后的条数冒充总数。
                            "available_columns_total": item.available_columns_total,
                        }
                        for item in high_issues
                    ],
                }
            ]
            logger.warning(
                "列名校验拦截",
                dialect=dialect_value,
                high_issue_count=len(high_issues),
                first_issue=high_issues[0].type,
            )

    logger.debug("节点完成", node="layer3_validate", elapsed_ms=round((time.monotonic() - _start) * 1000))
    update = {
        "sql_valid": len(errors) == 0,
        "validation_errors": errors,
        "validation_warnings": [],
        "transpiled_sql": sql,
    }
    from src.graph.context import with_execution_context

    return with_execution_context(state, update)


# 方法作用：用权威全量快照复核阶段一的高置信问题，消除裁剪造成的假阳性。
# Args: sql - 待校验 SQL；datasource - 数据源名；dialect - 方言；high_issues - 阶段一问题；
#       tenant_id - 显式租户编号；tables_hint - 阶段一已知属性，仅用于补充快照缺失属性。
# Returns: 复核后仍存在的问题列表；复核不可用时返回 None（回落阶段一结果）。
async def _recheck_with_full_snapshot(
    sql: str,
    datasource: str,
    dialect: str,
    high_issues: list,
    tenant_id: int | None = None,
    tables_hint: list[dict] | None = None,
) -> list | None:
    """失败安全：任何异常都回落阶段一结果，不因复核失败阻断链路。"""
    logger.debug(
        "阶段二复核入口",
        datasource=datasource,
        high_issue_count=len(high_issues),
        tenant_id=tenant_id,
    )
    try:
        from src.knowledge.schema_manager import get_schema_manager

        snapshot = await get_schema_manager().get_or_fetch_schema(
            datasource, user_query="", tenant_id=tenant_id
        )
        known_columns = {
            (str(table.get("name", "")), str(column.get("name", ""))): column
            for table in (tables_hint or [])
            for column in table.get("columns", [])
            if isinstance(column, dict)
        }
        # 全量快照决定表列是否存在；缺失类型/格式不等于否定阶段一已确认的证据。
        tables = [
            {
                "name": table.name,
                "columns": [
                    {
                        "name": column.name,
                        "type": getattr(column, "type", "") or known_columns.get((table.name, column.name), {}).get("type", ""),
                        "value_format": getattr(column, "value_format", "") or (
                            known_columns.get((table.name, column.name), {}).get("value_format", "")
                            if not getattr(column, "type", "") or getattr(column, "type", "") == known_columns.get((table.name, column.name), {}).get("type")
                            else ""
                        ),
                    }
                    for column in table.columns
                ],
            }
            for table in (snapshot.tables if snapshot else [])
        ]
        if not tables:
            logger.warning("阶段二复核跳过", reason="全量快照为空", datasource=datasource)
            return None
        from src.security.sql_schema_validation import validate_sql_schema

        issues = validate_sql_schema(
            sql,
            datasources=[datasource],
            dialect=dialect,
            tables=tables,
            stage="stage2",
        )
        confirmed = [item for item in issues if item.confidence == "high"]
        logger.info(
            "阶段二复核完成",
            datasource=datasource,
            before=len(high_issues),
            after=len(confirmed),
        )
        return confirmed
    except Exception as exc:
        logger.warning(
            "阶段二复核失败，回落阶段一结果",
            datasource=datasource,
            error=str(exc),
            exc_info=True,
        )
        return None
