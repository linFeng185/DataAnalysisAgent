"""multi_source — 多数据源并行调度 + LLM 跨源合并分析。

当用户同时选择多个数据源时，对每个源独立执行分析流水线（并行），
最后用 LLM 合并所有结果生成统一的跨源分析报告。

架构:
  multi_source_dispatch → [worker_1 | worker_2 | ...] → merge_results
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal, InvalidOperation

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)

_SOURCES_KEY = "multi_source_results"


async def multi_source_dispatch_node(state: AnalysisState) -> dict:
    """多数据源调度节点——对每个源并行执行独立分析流水线。

    仅在 state.selected_datasources 长度 > 1 时生效。
    每个 worker 运行：retrieve_schema → generate_sql → rewrite → execute。
    用 asyncio.gather 并行执行，return_exceptions=True 保证单源失败不阻塞。

    Args:
        state: AnalysisState

    Returns: {"multi_source_results": [{datasource, success, sql, data, ...}, ...]}
    """
    _start = time.monotonic()
    sources = state.get("selected_datasources", []) or []
    query = state.get("user_query", "")

    if len(sources) <= 1:
        logger.debug("多源调度跳过（单数据源）")
        return {_SOURCES_KEY: []}

    logger.info("多源调度开始", sources=len(sources), query=query[:60])
    logger.info(
        "多源调度计划",
        selected_sources=sources,
        scheduled_sources=sources,
        scheduled_count=len(sources),
    )

    # 并行执行各数据源分析
    coros = [_analyze_one(s, state) for s in sources]
    raw = await asyncio.gather(*coros, return_exceptions=True)

    # 收集有效结果
    collected: list[dict] = []
    for i, r in enumerate(raw):
        if isinstance(r, Exception):
            logger.warning("多源 worker 异常", source=sources[i], error=str(r))
            collected.append({"datasource": sources[i], "success": False, "error": str(r)})
        elif r:
            collected.append(r)

    elapsed = round((time.monotonic() - _start) * 1000)
    ok = sum(1 for r in collected if r.get("success"))
    logger.info("多源调度完成", total=len(sources), success=ok, elapsed_ms=elapsed)
    return {_SOURCES_KEY: collected}


async def _analyze_one(datasource: str, state: AnalysisState) -> dict | None:
    """单数据源完整分析流水线。

    对指定数据源执行：Schema 检索 → SQL 生成 → 方言重写 → 执行。
    异常在此捕获并返回 error dict，不向上传播。

    Args:
        datasource: 数据源名称
        state: AnalysisState

    Returns: {"datasource", "success", "sql", "data", "dialect", "tables"} 或 None
    """
    logger.debug("单源分析入口", datasource=datasource, query=state.get("user_query", "")[:60])
    try:
        # 在进入 Schema 与 LLM 链路前只做一次连接解析，失败立即返回。
        from src.datasource.registry import get_registry
        resolved = await get_registry().resolve_or_none(datasource)
        if resolved is None:
            logger.warning("单源分析跳过", datasource=datasource, reason="数据源连接失败或不存在")
            return {"datasource": datasource, "success": False, "error": "数据源连接失败或不存在"}

        # Schema 检索
        from src.graph.nodes.retrieve_schema import retrieve_schema_node
        s1 = dict(state)
        s1["datasource"] = datasource
        datasource_access = state.get("datasource_access", {}) or {}
        if datasource_access:
            permission = datasource_access.get(datasource)
            if permission is None:
                logger.warning("单源分析权限拒绝", datasource=datasource)
                return {"datasource": datasource, "success": False, "error": "无权访问数据源"}
            s1["allowed_columns"] = list(permission.get("allowed_columns", []) or [])
            s1["row_filter_sql"] = str(permission.get("row_filter_sql", "") or "")
            logger.info(
                "单源分析权限已注入",
                datasource=datasource,
                allowed_columns=len(s1["allowed_columns"]),
                has_row_filter=bool(s1["row_filter_sql"]),
            )
        s1["resolved_schema"] = resolved.schema
        s1["dialect"] = resolved.dialect
        r1 = await retrieve_schema_node(s1)
        if not r1.get("relevant_tables"):
            logger.info("单源分析完成", datasource=datasource, success=False, reason="无可用表结构")
            return {"datasource": datasource, "success": False, "error": "无可用表结构"}

        # SQL 生成
        from src.graph.nodes.generate_sql import generate_sql_node
        s2 = {**s1, **r1}
        global_query = str(state.get("user_query", "") or "")
        s2["user_query"] = (
            "这是多数据源并行分析中的单源 SQL 子任务。"
            f"当前只负责数据源 `{datasource}`，当前 Schema 也只属于该数据源；"
            "其他已选数据源由独立 worker 查询，最终由合并节点统一比较。"
            "请只基于当前 Schema 生成回答全局问题所需的本数据源查询；"
            "多个指标按全局问题中的顺序输出，并使用跨源稳定的英文 snake_case 指标别名；"
            "不要尝试跨库查询，也不要因为缺少其他数据源的 Schema 而返回空 SQL。"
            f"\n全局问题：{global_query}"
        )
        logger.info(
            "多源单库 SQL 生成状态",
            datasource=datasource,
            selected_sources=state.get("selected_datasources", []) or [],
            global_query=global_query[:200],
            worker_query=str(s2.get("user_query", ""))[:200],
            worker_table_count=len(s2.get("relevant_tables", []) or []),
        )
        r2 = await generate_sql_node(s2, {})
        sql = r2.get("generated_sql", "") if isinstance(r2, dict) else ""
        if not sql:
            logger.info("单源分析完成", datasource=datasource, success=False, reason="SQL 生成失败")
            return {"datasource": datasource, "success": False, "error": "SQL 生成失败"}

        # 安全校验
        dialect = r1.get("dialect", "mysql")
        from src.graph.nodes.layer3_validate import layer3_validate_node
        v3 = await layer3_validate_node({**s2, "generated_sql": sql, "dialect": dialect})
        validation_errors = v3.get("validation_errors", []) or []
        if validation_errors:
            error_message = "; ".join(
                str(error.get("message", "SQL 校验失败"))
                for error in validation_errors
            )
            logger.warning(
                "多源 SQL 校验失败，终止来源执行",
                datasource=datasource,
                dialect=dialect,
                error=error_message[:500],
            )
            return {
                "datasource": datasource,
                "success": False,
                "error": error_message,
                "validation_errors": validation_errors,
            }

        # 真实 EXPLAIN 语义校验，与单源主链保持一致。
        from src.graph.nodes.layer4_explain import layer4_explain_node
        v4 = await layer4_explain_node({
            **s2,
            "generated_sql": sql,
            "dialect": dialect,
            "datasource": datasource,
        })
        explain_errors = v4.get("explain_errors", []) or []
        if explain_errors:
            error_message = "; ".join(
                str(error.get("message", "EXPLAIN 校验失败"))
                for error in explain_errors
            )
            logger.warning(
                "多源 EXPLAIN 失败，终止来源执行",
                datasource=datasource,
                dialect=dialect,
                error=error_message[:500],
            )
            return {
                "datasource": datasource,
                "success": False,
                "error": error_message,
                "explain_errors": explain_errors,
            }
        sql = str(v4.get("generated_sql", "") or sql)

        # 执行
        from src.graph.nodes.execute_sql import execute_sql_node
        r4 = await execute_sql_node({**s2, "generated_sql": sql, "dialect": dialect})
        data = r4.get("query_result_sample", []) or []

        final_sql = str(r4.get("generated_sql", "") or sql)
        result = {"datasource": datasource, "success": not r4.get("execution_error"),
                  "sql": final_sql, "data": data[:50], "dialect": dialect,
                  "tables": len(r1.get("relevant_tables", []))}
        logger.info(
            "单源分析完成",
            datasource=datasource,
            success=result["success"],
            data_rows=len(result["data"]),
            final_sql_chars=len(final_sql),
        )
        return result
    except Exception as e:
        logger.error("单源分析异常", datasource=datasource, error=str(e), exc_info=True)
        return {"datasource": datasource, "success": False, "error": str(e)}


# 识别跨源单行可加指标，并用 Decimal 生成确定性总计。
# Args: query - 用户问题；successful_results - 查询成功的来源结果。
# Returns: 可直接写入 analysis_result 的精确汇总；不适用时返回 None。
def _build_cross_source_aggregation(
    query: str,
    successful_results: list[dict],
) -> dict | None:
    """仅对明确要求汇总且每个来源返回单行的公共数值列求和。"""
    logger.debug(
        "跨源精确汇总识别入口",
        query=query[:80],
        source_count=len(successful_results),
    )
    try:
        normalized_query = query.lower()
        additive_keywords = (
            "汇总", "合计", "总数", "总量", "总额", "总和",
            "grand total", "overall total",
        )
        comparison_keywords = (
            "比较", "对比", "分布", "排名", "占比", "趋势", "中位", "平均",
            "有什么不同", "有何不同", "差异", "区别", "哪个更",
        )
        matched_additive = [
            keyword for keyword in additive_keywords if keyword in normalized_query
        ]
        matched_comparison = [
            keyword for keyword in comparison_keywords if keyword in normalized_query
        ]
        logger.info(
            "跨源汇总意图探针",
            matched_additive=matched_additive,
            matched_comparison=matched_comparison,
            query=query[:120],
        )
        if not matched_additive:
            logger.info("跨源精确汇总跳过", reason="用户未要求可加汇总")
            return None
        if matched_comparison:
            logger.info("跨源精确汇总跳过", reason="用户要求比较或分布分析")
            return None
        if len(successful_results) < 2:
            logger.info("跨源精确汇总跳过", reason="成功来源少于两个")
            return None
        if any(len(result.get("data", []) or []) != 1 for result in successful_results):
            logger.info("跨源精确汇总跳过", reason="存在非单行来源结果")
            return None

        aligned_rows = _normalize_single_row_numeric_metrics(successful_results)
        source_rows = aligned_rows or [result["data"][0] for result in successful_results]
        first_columns = [key for key in source_rows[0] if key != "_datasource"]
        numeric_columns: list[str] = []
        decimal_values: dict[str, list[Decimal]] = {}
        normalized_rows: list[dict] = []
        for column in first_columns:
            if not all(column in row for row in source_rows):
                continue
            values = [row[column] for row in source_rows]
            if any(isinstance(value, bool) or not isinstance(value, (int, float, Decimal))
                   for value in values):
                continue
            try:
                decimal_values[column] = [Decimal(str(value)) for value in values]
                numeric_columns.append(column)
            except (InvalidOperation, ValueError):
                logger.warning("跨源数值列转换失败", column=column)

        if not numeric_columns:
            single_metrics: list[tuple[str, int | float | Decimal]] = []
            for row in source_rows:
                candidates = [
                    (column, value)
                    for column, value in row.items()
                    if column != "_datasource"
                    and not isinstance(value, bool)
                    and isinstance(value, (int, float, Decimal))
                ]
                if len(candidates) != 1:
                    logger.info(
                        "跨源精确汇总跳过",
                        reason="来源结果不满足单数值列别名归一化",
                    )
                    return None
                single_metrics.append(candidates[0])

            aliases = [column for column, _ in single_metrics]
            canonical_column = max(aliases, key=aliases.count)
            try:
                decimal_values[canonical_column] = [
                    Decimal(str(value)) for _, value in single_metrics
                ]
            except (InvalidOperation, ValueError):
                logger.warning("跨源单数值列转换失败", aliases=aliases)
                return None
            numeric_columns = [canonical_column]
            normalized_rows = [
                {canonical_column: value} for _, value in single_metrics
            ]
            logger.info(
                "跨源数值列别名归一化完成",
                aliases=aliases,
                canonical_column=canonical_column,
            )
        else:
            normalized_rows = [
                {column: row[column] for column in numeric_columns}
                for row in source_rows
            ]

        totals: dict[str, int | float] = {}
        for column in numeric_columns:
            total = sum(decimal_values[column], Decimal(0))
            totals[column] = (
                int(total) if total == total.to_integral_value() else float(total)
            )

        metric_summary = "；".join(
            f"{column} 总计 {value}" for column, value in totals.items()
        )
        source_insights = []
        for result, row in zip(successful_results, normalized_rows, strict=True):
            values_text = "，".join(
                f"{column}={row[column]}" for column in numeric_columns
            )
            source_insights.append(f"{result['datasource']}: {values_text}")

        analysis = {
            "summary": f"已汇总 {len(successful_results)} 个数据源：{metric_summary}。",
            "insights": source_insights,
            "recommended_chart_type": "bar",
            "follow_up_questions": [],
            "processor_name": "cross_source_aggregation",
            "cross_source_totals": totals,
            "_normalized_rows": normalized_rows,
        }
        logger.info(
            "跨源精确汇总完成",
            source_count=len(successful_results),
            totals=totals,
        )
        return analysis
    except Exception as exc:
        logger.error("跨源精确汇总失败", error=str(exc), exc_info=True)
        return None


# 判断跨源结果列是维度列、数值指标列还是全空未知列。
# Args: rows - 单个数据源的结果行；column - 待识别列名。
# Returns: dimension、metric 或 unknown。
def _classify_result_column(rows: list[dict], column: str) -> str:
    """使用该来源全部非空值识别列角色，避免只看第一行造成误判。"""
    logger.debug("跨源结果列角色识别入口", column=column, row_count=len(rows))
    values = [row.get(column) for row in rows if row.get(column) is not None]
    if not values:
        logger.info("跨源结果列角色识别完成", column=column, role="unknown")
        return "unknown"
    is_metric = all(
        not isinstance(value, bool) and isinstance(value, (int, float, Decimal))
        for value in values
    )
    role = "metric" if is_metric else "dimension"
    logger.info("跨源结果列角色识别完成", column=column, role=role)
    return role


# 为同一位置的跨源列选择稳定规范别名。
# Args: aliases - 各数据源同位置的原始列名。
# Returns: 规范化后的唯一列名。
def _choose_canonical_column(aliases: list[str]) -> str:
    """优先选择多数别名，平票时保持首个来源的可读别名。"""
    logger.debug("跨源规范列名选择入口", aliases=aliases)
    canonical = max(
        aliases,
        key=lambda alias: (
            aliases.count(alias),
            int(alias.isidentifier()),
            int(alias.startswith(("total_", "average_", "avg_", "count_"))),
            -aliases.index(alias),
        ),
    )
    logger.info("跨源规范列名选择完成", aliases=aliases, canonical=canonical)
    return canonical


# 按维度/指标角色序列对齐跨源结果，支持任意数量结果行和数值指标。
# Args: successful_results - 已成功执行且包含结果数据的来源列表。
# Returns: 每个来源对应的规范化结果行；结构不兼容时返回 None。
def _normalize_cross_source_rows(
    successful_results: list[dict],
) -> list[list[dict]] | None:
    """仅在列宽和角色序列兼容时按列位置统一别名。"""
    logger.debug("跨源结果列契约对齐入口", source_count=len(successful_results))
    try:
        if len(successful_results) < 2:
            logger.info("跨源结果列契约对齐跳过", reason="成功来源少于两个")
            return None

        source_rows = [list(result.get("data", []) or []) for result in successful_results]
        if any(not rows for rows in source_rows):
            logger.info("跨源结果列契约对齐跳过", reason="存在空来源结果")
            return None
        if any(any(not isinstance(row, dict) for row in rows) for rows in source_rows):
            logger.info("跨源结果列契约对齐跳过", reason="来源行不是字典")
            return None

        source_columns = [
            [column for column in rows[0] if column != "_datasource"]
            for rows in source_rows
        ]
        column_width = len(source_columns[0])
        if column_width == 0 or any(
            len(columns) != column_width for columns in source_columns
        ):
            logger.info("跨源结果列契约对齐跳过", reason="来源列数量不一致")
            return None
        if any(
            any(set(row) - {"_datasource"} != set(columns) for row in rows)
            for rows, columns in zip(source_rows, source_columns, strict=True)
        ):
            logger.info("跨源结果列契约对齐跳过", reason="同一来源结果行字段不一致")
            return None

        source_roles = [
            [_classify_result_column(rows, column) for column in columns]
            for rows, columns in zip(source_rows, source_columns, strict=True)
        ]
        resolved_roles: list[str] = []
        for index in range(column_width):
            concrete_roles = {
                roles[index] for roles in source_roles if roles[index] != "unknown"
            }
            if len(concrete_roles) > 1:
                logger.warning(
                    "跨源结果列契约对齐跳过",
                    reason="同位置列角色冲突",
                    position=index,
                    roles=[roles[index] for roles in source_roles],
                )
                return None
            resolved_roles.append(next(iter(concrete_roles), "dimension"))

        aliases_by_position = [
            [columns[index] for columns in source_columns]
            for index in range(column_width)
        ]
        canonical_columns = [
            _choose_canonical_column(aliases) for aliases in aliases_by_position
        ]
        if len(set(canonical_columns)) != column_width:
            logger.warning(
                "跨源结果列契约对齐跳过",
                reason="规范列名冲突",
                canonical_columns=canonical_columns,
            )
            return None

        normalized_batches = []
        for rows, columns in zip(source_rows, source_columns, strict=True):
            normalized_batches.append([
                {
                    canonical_columns[index]: row.get(columns[index])
                    for index in range(column_width)
                }
                for row in rows
            ])

        logger.info(
            "跨源结果列契约对齐完成",
            aliases=aliases_by_position,
            roles=resolved_roles,
            canonical_columns=canonical_columns,
            source_row_counts=[len(rows) for rows in source_rows],
        )
        return normalized_batches
    except Exception as exc:
        logger.error("跨源结果列契约对齐失败", error=str(exc), exc_info=True)
        return None


# 对齐跨源单行指标，兼容精确汇总调用方。
# Args: successful_results - 已成功执行且每个来源恰好一行的结果列表。
# Returns: 对齐后的单行列表；不满足条件时返回 None。
def _normalize_single_row_numeric_metrics(
    successful_results: list[dict],
) -> list[dict] | None:
    """复用通用列契约对齐，并提取每个来源的唯一结果行。"""
    logger.debug("跨源单行指标对齐入口", source_count=len(successful_results))
    if any(len(result.get("data", []) or []) != 1 for result in successful_results):
        logger.info("跨源单行指标对齐跳过", reason="来源结果不是单行")
        return None
    batches = _normalize_cross_source_rows(successful_results)
    if batches is None:
        logger.info("跨源单行指标对齐跳过", reason="列契约不兼容")
        return None
    rows = [batch[0] for batch in batches]
    logger.info("跨源单行指标对齐完成", source_count=len(rows))
    return rows


# 方法作用：把成功数据源的 SQL 和样本组装为跨源分析上下文。
# Args: query - 用户问题；results - 成功的数据源结果。
# Returns: 供 LLM 综合分析的文本上下文。
def _build_merge_context(query: str, results: list[dict]) -> str:
    logger.debug("构建跨源分析上下文入口", source_count=len(results))
    parts = [f"## 用户问题\n{query}\n"]
    for result in results:
        parts.append(
            f"### 数据源: {result['datasource']} ({result.get('dialect', '')})",
        )
        parts.append(f"SQL: {result.get('sql', '')[:300]}")
        parts.append(f"数据 (前3行): {result.get('data', [])[:3]}")
        parts.append(f"总行数: {len(result.get('data', []))}")
    context = "\n\n".join(parts)
    logger.info("构建跨源分析上下文完成", source_count=len(results), chars=len(context))
    return context


# 方法作用：按精确对齐、规范批次或原始结果的优先级生成统一跨源数据行。
# Args: results - 成功的数据源结果；normalized_rows - 单行指标对齐结果；normalized_batches - 批次对齐结果。
# Returns: 带 `_datasource` 来源字段的统一数据行。
def _collect_merged_rows(
    results: list[dict],
    normalized_rows: list[dict] | None,
    normalized_batches: list[list[dict]] | None,
) -> list[dict]:
    logger.debug(
        "收集跨源结果行入口",
        source_count=len(results),
        has_rows=normalized_rows is not None,
        has_batches=normalized_batches is not None,
    )
    if normalized_rows is not None:
        rows = [
            {"_datasource": result["datasource"], **dict(row)}
            for result, row in zip(results, normalized_rows, strict=True)
        ]
    elif normalized_batches is not None:
        rows = []
        for result, batch in zip(results, normalized_batches, strict=True):
            rows.extend(
                {"_datasource": result["datasource"], **dict(row)}
                for row in batch[:30]
            )
    else:
        rows = [
            {"_datasource": result["datasource"], **dict(row)}
            for result in results
            for row in result.get("data", [])[:30]
        ]
    logger.info("收集跨源结果行完成", rows=len(rows))
    return rows


# 方法作用：在最终分析摘要中补充所有失败数据源，避免下游分析覆盖告警。
# Args: analysis - 当前分析结果；failed_results - 失败数据源结果。
# Returns: 已补充失败来源说明的分析结果。
def _append_source_failures(analysis: dict, failed_results: list[dict]) -> dict:
    logger.debug("补充跨源失败摘要入口", failed=len(failed_results))
    if not failed_results:
        logger.info("补充跨源失败摘要完成", changed=False)
        return analysis
    failed_names = ", ".join(
        str(result.get("datasource", "未知来源")) for result in failed_results
    )
    failure_note = f"{len(failed_results)} 个数据源查询失败: {failed_names}。"
    current_summary = str(analysis.get("summary", "")).strip()
    result = analysis
    if failure_note not in current_summary:
        result = {**analysis, "summary": f"{current_summary} {failure_note}".strip()}
    logger.info("补充跨源失败摘要完成", changed=result is not analysis)
    return result


async def merge_results_node(state: AnalysisState) -> dict:
    """多数据源结果合并节点——用 LLM 综合分析所有源的结果。

    将各数据源的查询结果 + SQL 拼入 Prompt，
    LLM 生成统一分析报告并标注数据来源。
    LLM 不可用时用规则拼接摘要。

    Args:
        state: AnalysisState

    Returns: {"analysis_result": dict, "chart_config": dict}
    """
    results = state.get(_SOURCES_KEY, []) or []
    query = state.get("user_query", "")
    logger.debug("多源结果合并入口", result_count=len(results), query=query[:60])
    if not results:
        logger.info("多源结果合并跳过", reason="无调度结果")
        return {}

    ok = [r for r in results if r.get("success")]
    fail = [r for r in results if not r.get("success")]
    logger.info(
        "跨源结果列探针",
        source_columns={
            str(result.get("datasource", "")): [
                list(row.keys()) for row in (result.get("data", []) or [])[:1]
                if isinstance(row, dict)
            ]
            for result in ok
        },
    )

    ctx = _build_merge_context(query, ok)

    # 基础摘要（LLM 不可用时使用）
    summary = f"已从 {len(ok)} 个数据源获取数据。"
    summary_from_llm = False
    if fail:
        summary += f" {len(fail)} 个数据源查询失败: {', '.join(r['datasource'] for r in fail)}。"

    precise_analysis = _build_cross_source_aggregation(query, ok)
    normalized_rows = None
    normalized_batches = None
    if precise_analysis is not None:
        normalized_rows = precise_analysis.pop("_normalized_rows", None)
    elif ok:
        normalized_batches = _normalize_cross_source_rows(ok)

    # 精确可加汇总不调用 LLM，其他跨源问题继续由 LLM 综合语义。
    if ok and precise_analysis is None:
        try:
            from src.llm.client import get_task_llm, is_task_llm_available
            if is_task_llm_available("multi_source_merge"):
                llm = get_task_llm("multi_source_merge", temperature=0, reasoning=False)
                from langchain_core.messages import SystemMessage, HumanMessage
                resp = await llm.ainvoke([
                    SystemMessage(content=(
                        "你是数据分析师。综合来自多个数据库的结果生成分析报告。"
                        "标注每个结论的数据来源。中文输出。")),
                    HumanMessage(content=f"{ctx}\n请综合分析并回答用户问题。")])
                summary = resp.content.strip() or summary
                summary_from_llm = bool(resp.content.strip())
        except Exception as e:
            logger.warning("LLM 跨源合并失败", error=str(e))

    # 有数据时走分析+图表流水线
    analysis = precise_analysis or {
        "summary": summary,
        "insights": [],
        "recommended_chart_type": "table",
    }
    chart = {"type": "table", "option": {}}
    all_data = _collect_merged_rows(ok, normalized_rows, normalized_batches)

    if all_data:
        try:
            from src.graph.nodes.analyze_result import analyze_result_node
            from src.graph.nodes.generate_chart import generate_chart_node
            merged_state = {"query_result_sample": all_data[:200],
                            "user_query": query,
                            "intent": state.get("intent", "query")}
            if precise_analysis is None:
                a_result = await analyze_result_node(merged_state)
                analysis = a_result.get("analysis_result", analysis)
                if summary_from_llm:
                    analysis = {**analysis, "summary": summary}
            c_result = await generate_chart_node({**merged_state, "analysis_result": analysis})
            chart = c_result.get("chart_config", chart)
        except Exception as e:
            logger.warning("跨源分析/图表生成失败", error=str(e))

    # 分析节点可能覆盖初始摘要，最终出口必须恢复失败来源信息。
    analysis = _append_source_failures(analysis, fail)

    logger.info(
        "多源结果合并完成",
        total=len(results),
        success=len(ok),
        failed=len(fail),
        data_rows=len(all_data),
    )
    truncated = len(all_data) > 200
    return {
        "analysis_result": analysis,
        "chart_config": chart,
        "query_result_sample": all_data[:200],
        "query_result_full_count": len(all_data),
        "query_result_truncated": truncated,
        "query_result_statistics": {
            "row_count": len(all_data),
            "truncated": truncated,
        },
    }
