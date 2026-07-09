"""4.2 classify_intent Node — 判断用户查询意图 (规则匹配 + Skill 激活)。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def classify_intent_node(state: AnalysisState) -> dict:
    """Phase 1 规则匹配; Phase 2 切换 LLM。"""
    _start = time.monotonic()
    logger.info("节点开始", node="classify_intent")
    ch = state.get("conversation_history", []) or []
    logger.info("对话历史检查", has_history=len(ch) > 0, turns=len(ch))
    q = state["user_query"].lower()

    if any(w in q for w in ("表结构", "有哪些表", "字段", "schema", "有哪些列",
                             "怎么用", "函数", "语法", "是什么意思", "什么是")):
        intent = "metadata"
    elif any(w in q for w in ("上传", "文件", "csv", "excel")):
        intent = "file_analysis"
    elif any(w in q for w in ("为什么", "原因", "归因")):
        intent = "attribution"
    elif any(w in q for w in ("趋势", "变化", "走势")):
        intent = "trend"
    elif any(w in q for w in ("排名", "top", "各品类", "分类")):
        intent = "aggregation"
    elif any(w in q for w in ("你好", "谢谢", "帮助", "功能", "能做什么", "你是谁")):
        intent = "chat"
    elif any(w in q for w in ("查", "多少", "统计", "总共", "列出", "看看",
                               "多少行", "销售额", "订单", "用户")):
        intent = "query"
    else:
        intent = await _llm_classify(q) or "chat"

    # Skill 匹配 (9.1.6 关键词+意图+表名三重匹配)
    activated_skills = []
    skill_prompt = ""
    skill_tools = []
    try:
        from src.skill_manager import get_skill_manager
        mgr = get_skill_manager()
        tables = [t.get("name", "") for t in state.get("relevant_tables", [])]
        activated_skills = mgr.match_skills(state["user_query"], intent, tables)
        if activated_skills:
            skill_prompt = mgr.build_skill_prompt(activated_skills)
            skill_tools = mgr.get_active_tools(activated_skills)
    except Exception as e:
        logger.warning("Skill 匹配失败", error=str(e))

    logger.info("节点完成", node="classify_intent", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {
        "intent": intent,
        "activated_skills": [s.name for s in activated_skills],
        "skill_prompt_override": skill_prompt,
        "skill_tools": skill_tools,
    }


async def _llm_classify(query: str) -> str | None:
    """LLM 意图分类——规则未命中时回退，复用 get_llm() 配置。"""
    try:
        from src.llm.client import get_llm, is_llm_available
        if not is_llm_available():
            return None
        llm = get_llm(temperature=0, reasoning=False)
        from langchain_core.messages import SystemMessage, HumanMessage
        resp = await llm.ainvoke([
            SystemMessage(content="意图分类器。只输出: query/aggregation/trend/attribution/metadata/chat/file_analysis"),
            HumanMessage(content=query)])
        text = (resp.content or "").strip().lower()
        valid = {"query", "aggregation", "trend", "attribution", "metadata", "chat", "file_analysis"}
        for w in text.split():
            if w in valid:
                return w
        return None
    except Exception:
        return None
    return {
        "intent": intent,
        "activated_skills": [s.name for s in activated_skills],
        "skill_prompt_override": skill_prompt,
        "skill_tools": skill_tools,
    }
