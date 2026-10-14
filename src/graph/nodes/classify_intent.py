"""4.2 classify_intent Node — 判断用户查询意图 (规则匹配 + Skill 激活)。"""

from __future__ import annotations

import re
import time

from src.graph.contracts import build_task_plan
from src.graph.state import AnalysisState
from src.llm.output_contracts import TaskPlanOutput
from src.logging_config import get_logger

logger = get_logger(__name__)

# 英文规则补充词表：只为既有类别提供有限、可解释的词组，不引入翻译模型或外部调用。
_METADATA_PHRASES_EN = (
    "available tables", "list tables", "list all tables", "show tables",
    "what tables", "which tables", "what columns", "which columns",
    "column types", "column names", "show columns", "list columns",
    "table structure", "table schema",
)
_ATTRIBUTION_CORRELATION_PHRASES_EN = (
    "correlation", "correlations", "correlated", "outlier", "outliers", "anomaly", "anomalies",
)
_ATTRIBUTION_CAUSE_PHRASES_EN = (
    "why", "reason", "reasons", "cause", "causes", "caused",
)
_TREND_PHRASES_EN = ("trend", "trends", "over time", "time series")
_AGGREGATION_PHRASES_EN = (
    "rank", "ranks", "ranked", "ranking", "by category", "by categories",
    "per category", "group by", "grouped by",
)
_CHAT_PHRASES_EN = (
    "hello", "hi", "thanks", "thank you", "what can you do", "who are you", "help",
)
# 历史追问词表只包含指向已有结果的词组，避免吞掉包含新统计目标的请求。
_HISTORY_PHRASES_EN = (
    "these results", "this result", "the results above", "results above",
    "previous result", "previous results", "previous analysis", "earlier results",
)
# 明确新查询信号：仅在历史指代同时命中时生效，让新统计目标优先于回放旧结果。
_NEW_QUERY_PHRASES_EN = (
    "ignore the previous", "ignore previous results", "ignore these results",
    "ignore the results above", "ignore the above",
    "forget the previous", "forget these results",
    "new query", "another query", "fresh query",
)
# 中文新查询信号只收要求重新执行的表达：忽略旧结果、重新查询/统计/计算。
_NEW_QUERY_PHRASES_ZH = (
    "忽略之前", "忽略上述", "忽略以上", "忽略前面",
    "忘记之前", "忘记上述", "不看之前", "不看上述",
    "新的查询", "新查询", "重新查询", "重新统计", "重新计算",
)
_QUERY_PHRASES_EN = (
    "how many", "how much", "total", "average", "count", "sum", "number of",
    "sales", "revenue",
)
# 统计兜底补充词表与中文统计词一致：仅用于模型返回 chat 时的确定性纠正。
_STATISTICAL_MARKERS_EN = (
    "how many", "how much", "total", "average", "count", "sum", "number of",
    "quantity", "amount", "aggregate",
)

# 英文词组正则缓存：同一词组只编译一次，正常请求路径不重复编译。
_EN_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


# 方法作用：按英文词边界匹配有限词组，支撑中英文规则回退一致性。
# Args: query - 已小写化的用户问题；phrases - 英文有限词组元组。
# Returns: 任一词组以完整单词或词组命中时为 True。
def _english_phrase_hit(query: str, phrases: tuple[str, ...]) -> bool:
    """英文匹配使用 ASCII 词边界，避免 count 命中 account、sum 命中 summary 等子串误报。"""
    for phrase in phrases:
        pattern = _EN_PATTERN_CACHE.get(phrase)
        if pattern is None:
            pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(phrase)}(?![a-z0-9_])")
            _EN_PATTERN_CACHE[phrase] = pattern
        if pattern.search(query):
            return True
    return False


# 方法作用：判断历史指代是否伴随明确的新查询信号，决定新统计目标是否优先。
# Args: query - 已小写化的用户问题。
# Returns: 命中放弃旧结果或要求新查询的信号时为 True。
def _explicit_new_query_request(query: str) -> bool:
    """词表只含要求重新执行的表达；仅汇总已有结果的问法不会命中。"""
    return (
        _english_phrase_hit(query, _NEW_QUERY_PHRASES_EN)
        or any(phrase in query for phrase in _NEW_QUERY_PHRASES_ZH)
    )


# 方法作用：融合规则与模型回退判定用户查询意图，产出任务计划与后续路由状态。
# Args: state - 分析状态，读取 user_query、conversation_history、file_ids、权限与路由字段。
# Returns: 含 intent、task_plan、routing_context、Skill 结果及可选授权数据源字段的状态增量。
async def classify_intent_node(state: AnalysisState) -> dict:
    """Phase 1 规则匹配; Phase 2 切换 LLM。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.debug("节点开始", node="classify_intent")
    ch = state.get("conversation_history", []) or []
    logger.info("对话历史检查", has_history=len(ch) > 0, turns=len(ch))
    q = contexts.request.user_query.lower()

    metadata_phrases = (
        "表结构", "有哪些表", "schema", "有哪些列", "有哪些字段",
        "字段类型", "字段含义", "字段说明", "字段是什么",
        "函数怎么用", "语法怎么用", "是什么意思", "什么是 schema",
    )
    from src.graph.indicator_catalog import is_indicator_catalog_query

    indicator_catalog_phrases = (
        "有哪些指标", "有哪些数据指标", "有什么指标", "指标有哪些", "指标清单", "指标列表", "支持哪些指标",
    )
    indicator_value_phrases = (
        "下降", "上升", "增长", "减少", "趋势", "同比", "环比", "排名", "最高", "最低",
    )
    logger.info(
        "意图规则判定边界",
        metadata_matches=[phrase for phrase in metadata_phrases if phrase in q],
        indicator_catalog_matches=[phrase for phrase in indicator_catalog_phrases if phrase in q],
        indicator_value_matches=[phrase for phrase in indicator_value_phrases if phrase in q],
        history_turns=len(ch),
    )
    indicator_catalog = is_indicator_catalog_query(q)
    function_help = "怎么用" in q and any(
        marker in q for marker in ("date_", "count(", "sum(", "avg(", "函数", "语法")
    )
    llm_plan = None
    if (
        indicator_catalog
        or any(w in q for w in metadata_phrases)
        or _english_phrase_hit(q, _METADATA_PHRASES_EN)
        or function_help
    ):
        intent = "metadata"
    elif state.get("file_ids") or any(w in q for w in ("上传", "文件", "附件", "csv", "excel")):
        intent = "file_analysis"
    elif (
        any(w in q for w in ("相关性", "相关系数", "相关关系", "异常值", "异常"))
        or _english_phrase_hit(q, _ATTRIBUTION_CORRELATION_PHRASES_EN)
    ):
        intent = "attribution"
    elif any(w in q for w in ("漏斗", "转化漏斗")):
        intent = "aggregation"
    elif (
        any(w in q for w in ("为什么", "原因", "归因"))
        or _english_phrase_hit(q, _ATTRIBUTION_CAUSE_PHRASES_EN)
    ):
        intent = "attribution"
    elif (
        any(w in q for w in ("趋势", "变化", "走势"))
        or _english_phrase_hit(q, _TREND_PHRASES_EN)
    ):
        intent = "trend"
    elif (
        any(w in q for w in ("排名", "top", "各品类", "分类"))
        or _english_phrase_hit(q, _AGGREGATION_PHRASES_EN)
    ):
        intent = "aggregation"
    elif (
        any(w in q for w in ("你好", "谢谢", "帮助", "功能", "能做什么", "你是谁"))
        or _english_phrase_hit(q, _CHAT_PHRASES_EN)
    ):
        intent = "chat"
    # 明确新查询信号与历史指代冲突时以新统计目标为准，交由后续普通统计分支判定。
    elif ch and not _explicit_new_query_request(q) and (
        any(w in q for w in ("刚才", "上一轮", "上一次", "之前", "刚刚", "这些数据", "这份数据",
                              "规律", "趋势", "总结", "发现", "说明了", "能看出",
                              "有什么规律", "什么规律", "之间存在", "之间有什么",
                              "分析一下这些", "怎么看", "你怎么", "你觉得"))
        or _english_phrase_hit(q, _HISTORY_PHRASES_EN)
    ):
        intent = "meta"
    elif (
        any(w in q for w in ("查", "多少", "统计", "总共", "列出", "看看",
                              "多少行", "销售额", "订单", "用户", "客户",
                              "消费", "找出", "显示", "哪些", "各", "每",
                              "平均", "最高", "最低", "占比", "对比"))
        or _english_phrase_hit(q, _QUERY_PHRASES_EN)
    ):
        intent = "query"
    elif ch:
        llm_plan = await _llm_classify(q, conversation_history=ch)
        intent = llm_plan.intent if llm_plan is not None else "meta"
    else:
        llm_plan = await _llm_classify(q)
        intent = llm_plan.intent if llm_plan is not None else "chat"

    # 短统计问题即使被模型误判为 chat，也必须进入 SQL 查询链路。
    statistical_markers = (
        "统计", "总数", "总量", "总金额", "数量", "多少", "合计", "汇总", "平均",
    )
    matched_statistical_markers = [marker for marker in statistical_markers if marker in q]
    # 英文统计词与中文一致参与兜底，匹配仍使用词边界避免 account/summary 等子串误报。
    matched_statistical_markers.extend(
        marker for marker in _STATISTICAL_MARKERS_EN if _english_phrase_hit(q, (marker,))
    )
    if intent == "chat" and matched_statistical_markers:
        logger.info(
            "统计类问题意图兜底",
            query_chars=len(q),
            previous_intent=intent,
            matched_markers=matched_statistical_markers,
        )
        intent = "query"
        # 丢弃 chat 的模型操作字段，避免后续任务计划仍被写成 conversation。
        llm_plan = None

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
    logger.debug(
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
    logger.debug("授权候选数据源选择入口", candidate_count=len(candidates), query_chars=len(query))
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
# Args: query - 用户原始问题；conversation_history - 用于判断追问语义的最近对话历史。
# Returns: 合法意图名称；模型不可用、输出无效或调用失败时返回 None。
async def _llm_classify(
    query: str,
    *,
    conversation_history: list[dict] | None = None,
) -> TaskPlanOutput | None:
    """LLM 意图分类——规则未命中时回退。"""
    logger.debug("LLM 意图分类入口", query_chars=len(query))
    try:
        from src.llm.client import is_task_llm_available
        if not is_task_llm_available("classify_intent"):
            logger.info("LLM 意图分类回退", reason="任务模型不可用")
            return None
        from src.llm.invocation import invoke_structured
        from src.llm.prompt_budget import PromptSection

        history_text = "\n".join(
            f"Q: {str(item.get('user_query', '') or '')[:200]}\n"
            f"A: {str(item.get('analysis_summary', '') or '')[:200]}"
            for item in list(conversation_history or [])[-3:]
            if isinstance(item, dict) and item.get("user_query")
        )
        sections = [PromptSection("query", query, priority=100, min_chars=100, max_chars=650)]
        if history_text:
            sections.append(PromptSection(
                "history",
                f"## 最近对话\n{history_text}",
                priority=80,
                min_chars=100,
                max_chars=900,
            ))
        result = await invoke_structured(
            "intent.classify",
            sections,
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
