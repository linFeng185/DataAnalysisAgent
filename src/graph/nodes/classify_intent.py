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

    datasource_update: dict = {}
    datasource_access = state.get("datasource_access", {}) or {}
    if (
        intent not in {"chat", "file_analysis", "meta"}
        and not str(state.get("datasource", "") or "").strip()
        and datasource_access
    ):
        selected_datasource = await _select_authorized_datasource(
            state["user_query"], datasource_access,
        )
        permission = datasource_access[selected_datasource]
        datasource_update = {
            "datasource": selected_datasource,
            "selected_datasources": [selected_datasource],
            "allowed_columns": list(permission.get("allowed_columns", []) or []),
            "row_filter_sql": str(permission.get("row_filter_sql", "") or ""),
        }
        logger.info(
            "授权候选数据源选择完成",
            datasource=selected_datasource,
            candidate_count=len(datasource_access),
            intent=intent,
        )

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
        **datasource_update,
    }


# 方法作用：使用 SQL 任务模型从服务端授权候选中选择最相关的数据源。
# Args: query - 用户问题；datasource_access - 已授权数据源描述和权限快照。
# Returns: 必定属于授权候选的数据源名称。
async def _select_authorized_datasource(
    query: str,
    datasource_access: dict[str, dict],
) -> str:
    """模型只接收授权候选；不可用或输出越界时执行确定性回退。

    Args:
        query: 用户的自然语言问题。
        datasource_access: API 解析完成的授权候选映射。

    Returns:
        选中的授权数据源名称。
    """
    candidates = list(datasource_access)
    logger.debug("授权候选数据源选择入口", candidate_count=len(candidates), query=query[:80])
    if not candidates:
        logger.error("授权候选数据源选择失败", reason="候选为空")
        raise PermissionError("没有可访问的数据源")
    if len(candidates) == 1:
        logger.info("授权候选数据源单项命中", datasource=candidates[0])
        return candidates[0]
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from src.llm.client import get_task_llm, is_task_llm_available

        if is_task_llm_available("generate_sql"):
            catalog = "\n".join(
                f"- {name}: {str(datasource_access[name].get('description', '') or '')}"
                for name in candidates
            )
            llm = get_task_llm("generate_sql", temperature=0, reasoning=False)
            response = await llm.ainvoke([
                SystemMessage(content=(
                    "你是数据源路由器。只能从候选名称中选择一个最适合回答问题的数据源，"
                    "只输出数据源名称，不输出解释。"
                )),
                HumanMessage(content=f"问题：{query}\n授权候选：\n{catalog}"),
            ])
            selected = str(response.content or "").strip().strip("`\"'")
            if selected in datasource_access:
                logger.info("授权候选数据源模型命中", datasource=selected)
                return selected
            logger.warning("授权候选数据源模型越界", selected=selected[:80])
    except Exception as exc:
        logger.error("授权候选数据源模型选择失败", error=str(exc), exc_info=True)

    normalized_query = query.lower()
    selected = candidates[0]
    best_rank = (-1, 0)
    for index, name in enumerate(candidates):
        description = str(datasource_access[name].get("description", "") or "").strip().lower()
        score = int(name.lower() in normalized_query) + int(
            bool(description) and description in normalized_query
        )
        rank = (score, -index)
        logger.debug("授权候选数据源回退评分", datasource=name, score=score)
        if rank > best_rank:
            selected = name
            best_rank = rank
    logger.info("授权候选数据源确定性回退", datasource=selected)
    return selected


# 方法作用：在规则未命中时使用任务模型识别意图，并在模型故障时安全降级。
# Args: query - 用户原始问题。
# Returns: 合法意图名称；模型不可用、输出无效或调用失败时返回 None。
async def _llm_classify(query: str) -> str | None:
    """LLM 意图分类——规则未命中时回退。"""
    logger.debug("LLM 意图分类入口", query=query[:80])
    try:
        from src.llm.client import get_task_llm, is_task_llm_available
        if not is_task_llm_available("classify_intent"):
            logger.info("LLM 意图分类回退", reason="任务模型不可用")
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
                logger.info("LLM 意图分类完成", intent=w)
                return w
        logger.warning("LLM 意图分类回退", reason="模型输出不在合法集合", output=text[:80])
        return None
    except Exception as exc:
        logger.error("LLM 意图分类失败，回退规则分类", error=str(exc), exc_info=True)
        return None
