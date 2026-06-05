"""4.2 classify_intent Node — 判断用户查询意图 (规则匹配 + Skill 激活)。"""

from __future__ import annotations

from src.graph.state import AnalysisState


async def classify_intent_node(state: AnalysisState) -> dict:
    """Phase 1 规则匹配; Phase 2 切换 LLM。"""
    q = state["user_query"].lower()

    if any(w in q for w in ("为什么", "原因", "归因")):
        intent = "attribution"
    elif any(w in q for w in ("趋势", "变化", "走势")):
        intent = "trend"
    elif any(w in q for w in ("排名", "top", "各品类", "分类")):
        intent = "aggregation"
    elif any(w in q for w in ("表结构", "有哪些表", "字段", "schema")):
        intent = "metadata"
    elif any(w in q for w in ("上传", "文件", "csv", "excel")):
        intent = "file_analysis"
    elif any(w in q for w in ("查", "多少", "统计", "总共")):
        intent = "query"
    else:
        intent = "chat"

    return {
        "intent": intent,
        "activated_skills": [],
        "skill_prompt_override": "",
        "skill_tools": [],
    }
