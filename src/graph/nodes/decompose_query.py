"""decompose_query — LLM 分解复杂问题为子步骤。简单查询直接跳过。"""

from __future__ import annotations

import json, time
from src.graph.state import AnalysisState
from src.logging_config import get_logger
logger = get_logger(__name__)


async def decompose_query_node(state: AnalysisState) -> dict:
    """LLM 判断是否需要分解。简单查询（<30字+简单关键词）直接跳过。

    Returns: {"needs_decompose": bool, "decompose_steps": [{"step":N,"question":"...","depends_on":[],"output_columns":[]}]}
    """
    _start = time.monotonic()
    query = state.get("user_query", "")

    # 简单查询跳过
    if len(query) < 30 and any(w in query for w in ("多少","统计","列出","排名","查一下","看看")):
        logger.info("查询分解跳过（简单问题）", query=query[:60])
        return {"needs_decompose": False, "decompose_steps": []}

    try:
        from src.config import get_settings
        s = get_settings()
        schema_hint = state.get("long_term_memories_text", "")[:800]

        import aiohttp
        payload = {
            "model": s.cheap_llm_model or s.llm_model,
            "messages": [
                {"role": "system", "content": (
                    "你是SQL查询规划器。判断是否需要多步分解。"
                    "单表查询/简单聚合/简单JOIN→false。"
                    "需要中间结果的复杂问题(先查A再基于A结果查B)→true。"
                    "输出JSON:{\"needs_decompose\":bool,\"steps\":[{\"step\":1,\"question\":\"子问题\",\"depends_on\":[],\"output_columns\":[\"id\"]}]}"
                )},
                {"role": "user", "content": f"表结构:{schema_hint}\n\n问题:{query}"},
            ],
            "temperature": 0, "max_tokens": 500, "stream": False,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{s.openai_base_url}/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {s.openai_api_key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return {"needs_decompose": False, "decompose_steps": []}
                data = await resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                result = _parse(text)

        elapsed = round((time.monotonic() - _start) * 1000)
        if result["needs_decompose"]:
            logger.info("查询已分解", steps=len(result["steps"]), elapsed_ms=elapsed)
        else:
            logger.info("查询无需分解", elapsed_ms=elapsed)
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
