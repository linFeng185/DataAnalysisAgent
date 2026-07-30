"""decompose_query — LLM 分解复杂问题为子步骤。简单查询直接跳过。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def decompose_query_node(state: AnalysisState) -> dict:
    """LLM 判断是否需要分解。有上下文/简单查询直接跳过，仅多步关键词触发 LLM。

    Returns: {"needs_decompose": bool, "decompose_steps": [{"step":N,"question":"...","depends_on":[],"output_columns":[]}]}
    """
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    query = contexts.request.user_query
    ch = state.get("conversation_history", []) or []

    # 快径1：有历史上下文 → 大概率是追问，不需要分解
    if ch:
        logger.info("查询分解跳过（有上下文）", query=query[:60])
        return {"needs_decompose": False, "decompose_steps": []}

    # 快径2：无多步关键词 → 不需要分解
    multi_step_kw = ("然后", "再根据", "接着", "第一步", "第二步", "分别查",
                     "先查", "再查", "基于上一个", "用上一个结果")
    if not any(w in query for w in multi_step_kw):
        logger.info("查询分解跳过（简单问题）", query=query[:60])
        return {"needs_decompose": False, "decompose_steps": []}

    # 只有明确含多步关键词的问题才走 LLM 分解
    try:
        from src.llm.client import is_task_llm_available
        if not is_task_llm_available("decompose_query"):
            return {"needs_decompose": False, "decompose_steps": []}
        schema_hint = _format_schema_hint(state.get("relevant_tables", []) or [])
        from src.llm.invocation import invoke_structured
        from src.llm.output_contracts import DecomposeOutput
        from src.llm.prompt_budget import PromptSection

        parsed = await invoke_structured(
            "query.decompose",
            [
                PromptSection(
                    "query",
                    f"## 用户问题\n{query}",
                    priority=100,
                    min_chars=200,
                    max_chars=700,
                ),
                PromptSection(
                    "schema",
                    f"## 表结构\n{schema_hint}",
                    priority=90,
                    min_chars=500,
                    max_chars=1400,
                ),
            ],
            output_model=DecomposeOutput,
        )
        steps = [step.model_dump() for step in parsed.steps]
        result = {
            "needs_decompose": parsed.needs_decompose and len(steps) > 1,
            "decompose_steps": steps,
        }
        elapsed = round((time.monotonic() - _start) * 1000)
        label = f"{len(result['decompose_steps'])}步" if result["needs_decompose"] else "无需"
        logger.info("查询分解完成", label=label, elapsed_ms=elapsed)
        return result
    except Exception as e:
        logger.error("查询分解失败，回退", error=str(e), exc_info=True)
        return {"needs_decompose": False, "decompose_steps": []}


def _parse(text: str) -> dict:
    try:
        from src.llm.output_contracts import DecomposeOutput, parse_json_model

        parsed = parse_json_model(text, DecomposeOutput)
    except Exception as exc:
        logger.warning("查询分解结构化输出回退", error=str(exc), exc_info=True)
        return {"needs_decompose": False, "decompose_steps": []}
    steps = [step.model_dump() for step in parsed.steps]
    return {
        "needs_decompose": parsed.needs_decompose and len(steps) > 1,
        "decompose_steps": steps,
    }


# 方法作用：为查询分解器提供当前数据源真实表名和字段，而不是知识库正文。
# Args: tables - retrieve_schema 输出的相关表列表。
# Returns: 最大 2000 字符的紧凑 Schema 提示。
def _format_schema_hint(tables: list[dict]) -> str:
    """构造多步规划使用的确定性 Schema 摘要。"""
    logger.debug("查询分解 Schema 格式化入口", table_count=len(tables))
    lines: list[str] = []
    for table in tables[:20]:
        name = str(table.get("name", "") or "")
        columns = ", ".join(
            str(column.get("name", "") or "")
            for column in (table.get("columns", []) or [])[:30]
            if column.get("name")
        )
        if name:
            lines.append(f"{name}({columns})")
    result = "\n".join(lines)[:2000]
    logger.info("查询分解 Schema 格式化完成", chars=len(result), table_count=len(tables))
    return result
