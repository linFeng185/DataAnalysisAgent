"""decompose_query — LLM 分解复杂问题为子步骤。简单查询直接跳过。"""

from __future__ import annotations

import json, time
from src.graph.state import AnalysisState
from src.logging_config import get_logger
logger = get_logger(__name__)


async def decompose_query_node(state: AnalysisState) -> dict:
    """LLM 判断是否需要分解。有上下文/简单查询直接跳过，仅多步关键词触发 LLM。

    Returns: {"needs_decompose": bool, "decompose_steps": [{"step":N,"question":"...","depends_on":[],"output_columns":[]}]}
    """
    _start = time.monotonic()
    query = state.get("user_query", "")
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
        from src.llm.client import get_task_llm, is_task_llm_available
        if not is_task_llm_available("decompose_query"):
            return {"needs_decompose": False, "decompose_steps": []}
        llm = get_task_llm("decompose_query", temperature=0, reasoning=False)
        schema_hint = _format_schema_hint(state.get("relevant_tables", []) or [])
        from langchain_core.messages import SystemMessage, HumanMessage
        resp = await llm.ainvoke([
            SystemMessage(content=(
                "你是SQL查询规划器。判断是否需要多步分解。"
                "单表查询/简单聚合/简单JOIN→false。"
                "需要中间结果的复杂问题(先查A再基于A结果查B)→true。"
                "输出JSON:{\"needs_decompose\":bool,\"steps\":[{\"step\":1,\"question\":\"子问题\",\"depends_on\":[],\"output_columns\":[\"id\"]}]}")),
            HumanMessage(content=f"表结构:{schema_hint}\n\n问题:{query}")])
        text = (resp.content or "").strip()
        result = _parse(text)
        elapsed = round((time.monotonic() - _start) * 1000)
        label = f"{len(result['decompose_steps'])}步" if result["needs_decompose"] else "无需"
        logger.info("查询分解完成", label=label, elapsed_ms=elapsed)
        return result
    except Exception as e:
        logger.warning("查询分解失败，回退", error=str(e))
        return {"needs_decompose": False, "decompose_steps": []}


def _parse(text: str) -> dict:
    try:
        s = text.index("{"); e = text.rindex("}") + 1
        data = json.loads(text[s:e])
    except (json.JSONDecodeError, ValueError):
        return {"needs_decompose": False, "decompose_steps": []}
    needs = data.get("needs_decompose", False)
    steps = data.get("steps", []) or []
    step_nums = {s.get("step") for s in steps if isinstance(s, dict)}
    valid = [{"step": s["step"], "question": s.get("question",""), "depends_on": s.get("depends_on",[]),
              "output_columns": s.get("output_columns",[])}
             for s in steps if isinstance(s, dict) and s.get("step") in step_nums
             and all(d in step_nums and d < s["step"] for d in (s.get("depends_on",[]) or []))]
    return {"needs_decompose": needs and len(valid) > 1, "decompose_steps": valid[:5]}


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
