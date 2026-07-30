"""4.2 classify_intent Node — 判断用户查询意图 (规则匹配 + Skill 激活)。"""

from __future__ import annotations

import time

from src.graph.contracts import build_task_plan
from src.graph.state import AnalysisState
from src.llm.output_contracts import TaskPlanOutput
from src.logging_config import get_logger

logger = get_logger(__name__)


async def classify_intent_node(state: AnalysisState) -> dict:
    """Phase 1 规则匹配; Phase 2 切换 LLM。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.info("节点开始", node="classify_intent")
    ch = state.get("conversation_history", []) or []
    logger.info("对话历史检查", has_history=len(ch) > 0, turns=len(ch))
    q = contexts.request.user_query.lower()

    metadata_phrases = (
        "表结构", "有哪些表", "schema", "有哪些列", "有哪些字段",
        "字段类型", "字段含义", "字段说明", "字段是什么",
        "函数怎么用", "语法怎么用", "是什么意思", "什么是 schema",
    )
    function_help = "怎么用" in q and any(
        marker in q for marker in ("date_", "count(", "sum(", "avg(", "函数", "语法")
    )
    llm_plan = None
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
        llm_plan = await _llm_classify(q)
        intent = llm_plan.intent if llm_plan is not None else "chat"

    preliminary_plan = build_task_plan(intent, query=contexts.request.user_query)
    if preliminary_plan.capability == "forecast" and intent == "chat":
        intent = "trend"
    elif preliminary_plan.capability == "report" and intent == "chat":
        intent = "query"

    datasource_update: dict = {}
    datasource_access = contexts.permission.datasource_access
    if (
        preliminary_plan.capability in {"sql_analysis", "forecast", "report"}
        and not contexts.routing.datasource.strip()
        and datasource_access
    ):
        selected_datasource = await _select_authorized_datasource(
            contexts.request.user_query, datasource_access,
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

    from src.graph.skill_activation import activate_skills

    skill_result = activate_skills({**state, "intent": intent})
    selected_sources = datasource_update.get(
        "selected_datasources",
        contexts.routing.selected_datasources,
    )
    task_plan = build_task_plan(
        intent,
        query=contexts.request.user_query,
        datasources=selected_sources,
    )
    if llm_plan is not None:
        task_plan.operation = (llm_plan.operation or task_plan.operation).strip()[:64] or task_plan.operation
        task_plan.confidence = llm_plan.confidence

    update = {
        "intent": intent,
        "task_plan": task_plan.model_dump(),
        **skill_result,
        **datasource_update,
    }
    from src.graph.context import build_routing_context

    update["routing_context"] = build_routing_context({**state, **update}).model_dump()
    logger.info(
        "节点完成",
        node="classify_intent",
        elapsed_ms=round((time.monotonic() - _start) * 1000),
        skill_stage=update["routing_context"]["skill_activation_stage"],
        skill_candidates=len(update["routing_context"]["skill_candidate_ids"]),
    )
    return update


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
        from src.llm.client import is_task_llm_available
        from src.llm.invocation import invoke_structured
        from src.llm.output_contracts import DatasourceSelectionOutput
        from src.llm.prompt_budget import PromptSection

        if is_task_llm_available("generate_sql"):
            catalog = "\n".join(
                f"- {name}: {str(datasource_access[name].get('description', '') or '')}"
                for name in candidates
            )
            structured = await invoke_structured(
                "datasource.select",
                [
                    PromptSection(
                        "query",
                        f"## 用户问题\n{query}",
                        priority=100,
                        min_chars=100,
                        max_chars=500,
                    ),
                    PromptSection(
                        "authorized_catalog",
                        f"## 授权候选\n{catalog}",
                        priority=90,
                        min_chars=300,
                        max_chars=900,
                    ),
                ],
                output_model=DatasourceSelectionOutput,
                task="generate_sql",
            )
            selected = structured.datasource
            if selected in datasource_access:
                logger.info("授权候选数据源模型命中", datasource=selected)
                return selected
            logger.warning("授权候选数据源模型越界", selected=selected[:80])
    except Exception as exc:
        from src.failure_policy import FailureDomain, fallback_allowed

        if not fallback_allowed(FailureDomain.LLM):
            raise
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
async def _llm_classify(query: str) -> TaskPlanOutput | None:
    """LLM 意图分类——规则未命中时回退。"""
    logger.debug("LLM 意图分类入口", query=query[:80])
    try:
        from src.llm.client import is_task_llm_available
        if not is_task_llm_available("classify_intent"):
            logger.info("LLM 意图分类回退", reason="任务模型不可用")
            return None
        from src.llm.invocation import invoke_structured
        from src.llm.prompt_budget import PromptSection

        result = await invoke_structured(
            "intent.classify",
            [PromptSection("query", query, priority=100, min_chars=100, max_chars=650)],
            output_model=TaskPlanOutput,
            task="classify_intent",
        )
        logger.info("LLM 意图分类完成", intent=result.intent, operation=result.operation)
        return result
    except Exception as exc:
        from src.failure_policy import FailureDomain, fallback_allowed

        if not fallback_allowed(FailureDomain.LLM):
            raise
        logger.error("LLM 意图分类失败，回退规则分类", error=str(exc), exc_info=True)
        return None
