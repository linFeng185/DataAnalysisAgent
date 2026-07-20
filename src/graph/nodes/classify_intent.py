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

    metadata_phrases = (
        "表结构", "有哪些表", "schema", "有哪些列", "有哪些字段",
        "字段类型", "字段含义", "字段说明", "字段是什么",
        "函数怎么用", "语法怎么用", "是什么意思", "什么是 schema",
    )
    function_help = "怎么用" in q and any(
        marker in q for marker in ("date_", "count(", "sum(", "avg(", "函数", "语法")
    )
    if any(w in q for w in metadata_phrases) or function_help:
        intent = "metadata"
    elif any(w in q for w in ("上传", "文件", "csv", "excel")):
        intent = "file_analysis"
    elif any(w in q for w in ("相关性", "相关系数", "相关关系", "异常值", "异常")):
        intent = "attribution"
    elif any(w in q for w in ("漏斗", "转化漏斗")):
        intent = "aggregation"
    elif any(w in q for w in ("为什么", "原因", "归因")):
        intent = "attribution"
    elif any(w in q for w in ("趋势", "变化", "走势")):
        intent = "trend"
    elif any(w in q for w in ("排名", "top", "各品类", "分类")):
        intent = "aggregation"
    elif any(w in q for w in ("你好", "谢谢", "帮助", "功能", "能做什么", "你是谁")):
        intent = "chat"
    elif ch and any(w in q for w in ("规律", "趋势", "总结", "发现", "说明了", "能看出",
                                      "有什么规律", "什么规律", "之间存在", "之间有什么",
                                      "分析一下这些", "怎么看", "你怎么", "你觉得")):
        intent = "meta"
    elif any(w in q for w in ("查", "多少", "统计", "总共", "列出", "看看",
                               "多少行", "销售额", "订单", "用户", "客户",
                               "消费", "找出", "显示", "哪些", "各", "每",
                               "平均", "最高", "最低", "占比", "对比")):
        intent = "query"
    elif ch:
        intent = "query"
    else:
        intent = await _llm_classify(q) or "chat"

    activated_skills = []
    skill_prompt = ""
    skill_tools = []
    try:
        from src.skill_manager import get_skill_manager
        mgr = get_skill_manager()
        tables = [t.get("name", "") for t in state.get("relevant_tables", [])]
        activated_skills = mgr.match_skills(
            state["user_query"],
            intent,
            tables,
            tenant_id=state.get("tenant_id"),
            user_id=state.get("user_id"),
        )
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
    """LLM 意图分类——规则未命中时回退。"""
    try:
        from src.llm.client import get_task_llm, is_task_llm_available
        if not is_task_llm_available("classify_intent"):
            return None
        llm = get_task_llm("classify_intent", temperature=0, reasoning=False)
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
