"""multi_source — 多数据源并行调度 + LLM 跨源合并分析。

当用户同时选择多个数据源时，对每个源独立执行分析流水线（并行），
最后用 LLM 合并所有结果生成统一的跨源分析报告。

架构:
  multi_source_dispatch → [worker_1 | worker_2 | ...] → merge_results
"""

from __future__ import annotations

import asyncio
import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)

_SOURCES_KEY = "multi_source_results"
_MAX_SOURCES = 5  # 最多并行分析的数据源数量（防止 LLM Token 爆炸）


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

    # 并行执行各数据源分析
    coros = [_analyze_one(s, state) for s in sources[:_MAX_SOURCES]]
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
    try:
        # Schema 检索
        from src.graph.nodes.retrieve_schema import retrieve_schema_node
        s1 = dict(state); s1["datasource"] = datasource
        r1 = await retrieve_schema_node(s1)
        if not r1.get("relevant_tables"):
            return {"datasource": datasource, "success": False, "error": "无可用表结构"}

        # SQL 生成
        from src.graph.nodes.generate_sql import generate_sql_node
        r2 = await generate_sql_node({**s1, **r1}, {})
        sql = (r2 or {}).get("generated_sql", "")
        if not sql:
            return {"datasource": datasource, "success": False, "error": "SQL 生成失败"}

        # 方言重写 + 执行
        dialect = r1.get("dialect", "mysql")
        from src.tools.sql_rewriter import rewrite_sql
        sql = rewrite_sql(sql, dialect)

        from src.graph.nodes.execute_sql import execute_sql_node
        r3 = await execute_sql_node({**s1, **r1, "generated_sql": sql, "dialect": dialect})
        data = r3.get("query_result_sample", []) or []

        return {"datasource": datasource, "success": not r3.get("execution_error"),
                "sql": sql, "data": data[:50], "dialect": dialect,
                "tables": len(r1.get("relevant_tables", []))}
    except Exception as e:
        logger.warning("单源分析异常", datasource=datasource, error=str(e))
        return {"datasource": datasource, "success": False, "error": str(e)}


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
    if not results:
        return {}

    ok = [r for r in results if r.get("success")]
    fail = [r for r in results if not r.get("success")]

    # 组装跨源分析上下文
    parts = [f"## 用户问题\n{query}\n"]
    for r in ok:
        parts.append(f"### 数据源: {r['datasource']} ({r.get('dialect','')})")
        parts.append(f"SQL: {r.get('sql', '')[:300]}")
        parts.append(f"数据 (前3行): {r.get('data', [])[:3]}")
        parts.append(f"总行数: {len(r.get('data', []))}")
    ctx = "\n\n".join(parts)

    # 基础摘要（LLM 不可用时使用）
    summary = f"已从 {len(ok)} 个数据源获取数据。"
    if fail:
        summary += f" {len(fail)} 个数据源查询失败: {', '.join(r['datasource'] for r in fail)}。"

    # LLM 跨源分析
    if ok:
        try:
            from src.llm.client import is_llm_available, get_llm
            if is_llm_available():
                llm = get_llm(temperature=0, reasoning=False)
                from langchain_core.messages import SystemMessage, HumanMessage
                resp = await llm.ainvoke([
                    SystemMessage(content=(
                        "你是数据分析师。综合来自多个数据库的结果生成分析报告。"
                        "标注每个结论的数据来源。中文输出。")),
                    HumanMessage(content=f"{ctx}\n请综合分析并回答用户问题。")])
                summary = resp.content.strip() or summary
        except Exception as e:
            logger.warning("LLM 跨源合并失败", error=str(e))

    return {"analysis_result": {"summary": summary, "insights": []},
            "chart_config": {"type": "table", "option": {}}}
