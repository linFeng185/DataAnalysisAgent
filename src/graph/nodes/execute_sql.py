"""4.7 execute_sql Node — 通过 DataSourceRegistry 执行 SQL。"""

from __future__ import annotations

import time

from src.config import get_settings
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def execute_sql_node(state: AnalysisState) -> dict:
    """Phase 2: registry → connector.execute()。Phase 1: 返回空数据 + 提示。"""
    _start = time.monotonic()
    logger.info("节点开始", node="execute_sql")
    sql = (state.get("generated_sql", "") or "").strip()
    ds_name = state.get("datasource", "")

    # SQL 方言重写 — 修复 LLM 常见语法错误（纯正则，非 LLM call）
    dialect = state.get("dialect", "")
    if sql and dialect:
        from src.tools.sql_rewriter import rewrite_sql
        sql = rewrite_sql(sql, dialect)

    # LLM 返回空 SQL（如问题无法用现有数据回答）时直接跳过执行
    if not sql:
        logger.info("SQL 为空，跳过数据库执行", datasource=ds_name)
        return {"query_result_sample": [], "query_result_full_count": 0,
                "query_result_statistics": {"row_count": 0}}

    # 12.2.1 频率限制检查
    from src.security.data_masker import check_rate_limit
    if not check_rate_limit():
        return {"execution_error": "请求频率超限",
                "query_result_sample": [], "query_result_full_count": 0,
                "query_result_statistics": {"row_count": 0}}

    # 行列级权限（多租户时生效）
    from src.config import get_settings
    if get_settings().multi_tenant:
        allowed = state.get("allowed_columns", []) or []
        rfilter = state.get("row_filter_sql", "") or ""
        if allowed:
            from src.security.permission_check import check_column_whitelist
            col_err_perm = check_column_whitelist(sql, allowed)
            if col_err_perm:
                logger.warning("列权限拦截", datasource=ds_name, error=col_err_perm)
                return {"query_result_sample": [], "query_result_full_count": 0,
                        "query_result_statistics": {"row_count": 0},
                        "generated_sql": sql, "execution_error": col_err_perm}
        if rfilter:
            from src.security.permission_check import inject_row_filter
            sql = inject_row_filter(sql, rfilter)

    # Layer 2: 列名验证
    col_err = _validate_column_references(sql, state.get("relevant_tables", []))
    if col_err:
        logger.warning("列名验证失败，触发重试", datasource=ds_name, error=col_err)
        return {"query_result_sample": [], "query_result_full_count": 0,
                "query_result_statistics": {"row_count": 0},
                "generated_sql": sql, "execution_error": col_err}

    # 尝试连接数据源
    try:
        from src.datasource.registry import get_registry
        registry = get_registry()
        ds = await registry.resolve_or_none(ds_name)
        if ds and ds.engine:
            # 直接使用已注入的 engine 执行
            import sqlalchemy as sa
            async with ds.engine.connect() as conn:
                # SQLite 跳过超时设置
                if ds.dialect != "sqlite":
                    settings = get_settings()
                    from src.config import get_settings as _gs
                    timeout_s = _gs().max_execution_time
                    if ds.dialect == "clickhouse":
                        await conn.execute(sa.text(f"SET max_execution_time = {timeout_s}"))
                    elif ds.dialect == "mysql":
                        await conn.execute(sa.text(f"SET SESSION max_execution_time = {timeout_s * 1000}"))
                    elif ds.dialect == "postgres":
                        await conn.execute(sa.text(f"SET statement_timeout = '{timeout_s * 1000}ms'"))

                result = await conn.execute(sa.text(sql))
                rows = [dict(row._mapping) for row in result]
            elapsed = round((time.monotonic() - _start) * 1000)
            # 12.3.3 审计日志
            import asyncio
            from src.security.data_masker import log_audit
            asyncio.create_task(log_audit("anonymous", ds_name, sql, len(rows), elapsed, True))
            logger.info("节点完成", node="execute_sql", elapsed_ms=elapsed)
            return {
                "generated_sql": sql,  # 同步重写后的 SQL 到前端
                "query_result_sample": rows[:200],
                "query_result_full_count": len(rows),
                "query_result_statistics": {"row_count": len(rows)},
                "execution_error": "",
            }
    except Exception as e:
        logger.warning("数据源执行失败", datasource=ds_name, error=str(e))
        err_msg = str(e)
        # 提取简洁错误信息
        if len(err_msg) > 300:
            # 截取 pymysql/mysql 错误的关键部分
            import re
            match = re.search(r'\((\d+), "([^"]+)"\)', err_msg)
            if match:
                err_msg = f"SQL 执行错误 [{match.group(1)}]: {match.group(2)}"
            else:
                err_msg = err_msg[:300]
        logger.info("节点完成", node="execute_sql", elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {
            "query_result_sample": [],
            "query_result_full_count": 0,
            "query_result_statistics": {"row_count": 0},
            "generated_sql": sql,
            "execution_error": err_msg,
        }

    logger.error("execute_sql 未预期路径", datasource=ds_name)
    return {"query_result_sample": [], "query_result_full_count": 0,
            "query_result_statistics": {"row_count": 0},
            "generated_sql": sql, "execution_error": f"数据源 '{ds_name}' 内部错误"}


def _validate_column_references(sql: str, tables: list[dict]) -> str | None:
    """执行前验证 SQL 中的列引用是否都存在于表结构中。

    与 should_retry 配合：验证失败 → execution_error → retry → LLM 修正。
    跳过 SELECT 中 AS 定义的别名（如 SUM(x) AS total_sales），这些不是表列。

    Args:
        sql - 待验证的 SQL
        tables - relevant_tables 列表

    Returns: 错误消息字符串，None 表示通过
    """
    if not tables:
        return None
    try:
        import sqlglot
        from sqlglot import exp

        valid_cols: set[str] = set()
        valid_tables: set[str] = set()
        for t in tables:
            tname = t.get("name", "")
            if tname:
                valid_tables.add(tname.lower())
                valid_tables.add(tname)
            for c in t.get("columns", []):
                cname = c.get("name", "")
                if cname:
                    valid_cols.add(cname.lower())
                    valid_cols.add(cname)
                    if tname:
                        valid_cols.add(f"{tname.lower()}.{cname.lower()}")

        if not valid_cols:
            return None

        tree = sqlglot.parse_one(sql, read="mysql")
        if not tree:
            return None

        # 收集 SELECT 中 AS 定义的别名，这些不是表列，不校验
        aliases: set[str] = set()
        for sel in tree.find_all(exp.Select):
            for col in sel.expressions:
                alias = col.alias_or_name if hasattr(col, 'alias_or_name') else col.alias
                if alias and alias != col.name:
                    aliases.add(alias.lower())

        errors: list[str] = []
        for node in tree.find_all(exp.Column):
            col_name = node.name
            if col_name == "*" or col_name == "1":
                continue
            # 跳过 SELECT 中定义的别名
            if col_name.lower() in aliases:
                continue
            if col_name.lower() not in valid_cols:
                table_name = node.table
                full_ref = f"{table_name}.{col_name}" if table_name else col_name
                if full_ref.lower() not in valid_cols:
                    err = f"列 '{full_ref}' 不在可用列中（可用: {sorted(valid_cols)[:15]}）"
                    if err not in errors:
                        errors.append(err)

        if errors:
            return "列名校验失败: " + "; ".join(errors[:3])
        return None
    except Exception:
        return None