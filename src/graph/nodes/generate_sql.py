"""
generate_sql 节点 — LLM 生成 SQL，支持错误回注重试。

这是流水线中最关键的节点。它负责：
  1. 将「表结构」+「方言参考」+「用户问题」组装成 LLM Prompt
  2. 调用 LLM 流式生成 SQL（通过 src/llm/client.py 工厂获取模型）
  3. 从 LLM 响应中鲁棒提取 SQL（支持 JSON / SQL 代码块 / 纯文本）
  4. 在重试模式下，将上一轮的 SQL 和错误信息注入 Prompt

数据流：
  输入:  relevant_tables（表结构）, user_query（用户问题）, dialect（方言）
        validation_errors + generated_sql（仅重试时）
  输出:  generated_sql（生成的 SQL）, retry_count

与条件路由的协作:
  after_layer3 检测到语法错误 → 回到 generate_sql 重试（retry_count+1）
  after_layer4 检测到 EXPLAIN 错误 → 同上
  should_retry 检测到执行错误 → 同上
  三类重试都回到这里，每次重试 error_context 会追加到 Prompt 中
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import Any

from langchain_core.runnables import RunnableConfig

from src.graph.state import AnalysisState
from src.llm.prompt_budget import PromptSection
from src.llm.prompts import get_dialect_cheatsheet
from src.logging_config import get_logger
from src.security.sql_execution import normalize_sql_dialect

logger = get_logger(__name__)

_PROMPT_SCHEMA_MAX_CHARS = 4400
_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "订单": ("order", "orders", "sale", "sales"),
    "金额": ("amount", "amt", "price", "revenue", "sales", "total"),
    "客单价": ("unit_price", "average_order_value", "aov", "price"),
    "用户": ("user", "users", "customer", "customers", "member"),
    "数量": ("count", "qty", "quantity", "number", "num"),
    "状态": ("status", "state"),
    "日期": ("date", "day", "time", "created", "updated"),
    "时间": ("time", "date", "created", "updated"),
    "产品": ("product", "sku", "item"),
    "库存": ("stock", "inventory", "qty", "quantity"),
}
_INTERNAL_COLUMN_MARKERS = {
    "internal", "remark", "remarks", "note", "notes", "debug", "trace",
    "created_by", "updated_by", "deleted_by", "private", "secret",
}


# 方法作用：延迟检查指定 LLM 任务是否可用，避免工作流装配时加载模型依赖。
# Args: task - LLM 任务标识。
# Returns: 指定任务存在可用模型时返回 True。
def _is_task_llm_available(task: str) -> bool:
    from src.llm.client import is_task_llm_available

    return is_task_llm_available(task)


# 方法作用：延迟创建指定任务模型并保留测试可替换的模块级边界。
# Args: task - LLM 任务标识；kwargs - 模型创建参数。
# Returns: 任务对应的 ChatModel。
def _get_task_llm(task: str, **kwargs: Any) -> Any:
    from src.llm.client import get_task_llm

    return get_task_llm(task, **kwargs)


# 方法作用：兼容旧测试和扩展点，同时按 generate_sql 任务检查模型可用性。
# Args: 无。
# Returns: 远程授权模型或本地模型可用于 SQL 生成时返回 True。
def is_llm_available() -> bool:
    """返回 SQL 生成任务的模型可用状态。"""
    logger.debug("SQL 任务模型可用性入口")
    available = _is_task_llm_available("generate_sql")
    logger.info("SQL 任务模型可用性完成", available=available)
    return available


# 方法作用：兼容旧 Mock 接口并创建关闭 reasoning 的 SQL 任务模型。
# Args: temperature - 生成温度；stream - 是否创建流式模型。
# Returns: 按 generate_sql 任务策略创建的 ChatModel。
def get_llm(temperature: float = 0, *, stream: bool = True):
    """创建 SQL 生成模型，强制关闭推理模式以降低延迟。"""
    logger.debug("创建 SQL 任务模型入口", temperature=temperature, stream=stream)
    model = _get_task_llm(
        "generate_sql",
        temperature=temperature,
        reasoning=False,
        stream=stream,
    )
    logger.info("创建 SQL 任务模型完成", stream=stream)
    return model


async def generate_sql_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    SQL 生成节点的主入口。

    读取 state 中的用户查询、表结构、方言信息，
    优先调用 LLM 流式生成 SQL，LLM 不可用时退回模板。

    返回: {"generated_sql": str, "retry_count": int}
    """
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.info("节点开始", node="generate_sql")
    tables = state.get("relevant_tables", [])
    dialect = contexts.execution.dialect or "clickhouse"
    query = contexts.request.user_query
    retry = contexts.execution.retry_count
    task_plan = contexts.routing.task_plan

    # 指标目录先按结构化计划处理，禁止把目录问题交给 LLM 编造跨表 SQL。
    if task_plan.get("operation") == "indicator_catalog":
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_catalog_sql,
            build_indicator_catalog_summary,
        )

        catalog_plan = build_indicator_catalog_plan(tables)
        sql = build_indicator_catalog_sql(catalog_plan, dialect)
        logger.info(
            "指标目录 SQL 规划完成",
            mode=catalog_plan.mode,
            requires_data_query=catalog_plan.requires_data_query,
            selected_table=catalog_plan.selected_table,
            sql_present=bool(sql),
        )
        return {
            "generated_sql": sql,
            "retry_count": retry + 1,
            "needs_input": catalog_plan.needs_input,
            "input_explanation": build_indicator_catalog_summary(catalog_plan),
            "needs_time_range": False,
            "time_range_explanation": "",
            "validation_errors": [],
            "explain_errors": [],
            "execution_error": "",
            "execution_error_type": "",
            "execution_retry_count": 0,
        }

    # 时间补参只接受 classify_intent 写入的结构化任务计划。
    if bool(task_plan.get("needs_time_range", False)):
        logger.info("需要用户指定时间范围", query=query[:80], source="task_plan")
        return {
            "generated_sql": "",
            "needs_input": True,
            "input_explanation": "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年 或 全部数据）",
            "needs_time_range": True,
            "time_range_explanation": "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年 或 全部数据）",
            "retry_count": retry + 1,
            "validation_errors": [],
            "execution_error": "",
            "execution_error_type": "",
        }

    # 表结构 → Markdown 格式（用于 Prompt）；白名单使用完整授权表，详细 Schema 按问题裁剪。
    prompt_tables = select_prompt_tables(
        tables,
        query,
        dialect=dialect,
        max_chars=_PROMPT_SCHEMA_MAX_CHARS,
    )
    schema_text = format_schema_for_prompt(prompt_tables, dialect)
    table_whitelist = build_table_whitelist_context(tables)
    logger.info(
        "SQL Schema Prompt 裁剪完成",
        total_tables=len(tables),
        prompt_tables=len(prompt_tables),
        schema_chars=len(schema_text),
        whitelist_chars=len(table_whitelist),
    )
    # 获取该方言的 SQL 语法速查表
    dialect_hint = get_dialect_cheatsheet(dialect)
    # 技能系统注入的额外 Prompt（Phase 2）
    skill_prompt = state.get("skill_prompt_override", "")
    # 业务规则、知识命中、枚举和已验证示例统一组装为证据块
    grounding_context = build_sql_grounding_context(state)

    # ── 重试时的错误上下文 ──
    error_context = build_retry_context(state, retry, tables)
    if retry > 0:
        prev_sql = str(state.get("generated_sql", "") or "")
        exec_err = str(state.get("execution_error", "") or "")
        val_errors = state.get("validation_errors", []) or []
        logger.info(
            "SQL 重试上下文边界",
            retry=retry,
            previous_sql_chars=len(prev_sql),
            validation_error_types=[
                str(error.get("type", "")) for error in val_errors if isinstance(error, dict)
            ],
            has_execution_error=bool(exec_err),
        )

    # 新轮次的 retry_count 由 prepare_turn 统一清零；节点内再次按历史判断会把同轮重试误判为新问题。
    history = state.get("conversation_history", []) or []
    # 回退：如果 conversation_history 为空，从 messages 字段提取上下文
    if not history:
        msgs = state.get("messages", []) or []
        if msgs:
            # 从 LangChain messages 重建简化的对话历史
            for msg in msgs:
                if hasattr(msg, 'content') and msg.content:
                    role = 'user' if msg.__class__.__name__ == 'HumanMessage' else 'assistant'
                    history.append({
                        "turn_id": len(history) + 1,
                        "user_query": msg.content if role == 'user' else '',
                        "analysis_summary": msg.content if role == 'assistant' else '',
                        "generated_sql": '',
                        "execution_success": True,
                        "chart_type": '',
                    })
        if history:
            logger.info("对话上下文（从 messages 复原）", turns=len(history))
    if history:
        logger.info("对话上下文", turns=len(history),
                    types=[type(t).__name__ for t in history])
    else:
        logger.info("对话上下文为空（可能是首轮或 checkpointer 反序列化失败）")
    # ── 主路径：LLM 流式生成 ──
    explanation = ""
    fallback_error = ""
    if is_llm_available():
        logger.info("SQL 生成: 调用 LLM", query=query[:80], dialect=dialect, retry=retry)
        sql, reasoning, explanation = await _llm_generate(
            schema_text=schema_text,
            table_whitelist=table_whitelist,
            dialect_hint=dialect_hint,
            dialect=dialect,
            query=query,
            error_ctx=error_context,
            skill_prompt=skill_prompt,
            grounding_context=grounding_context,
            config=config,
            conversation_history=history,
        )
        if sql.startswith("-- LLM"):
            fallback_error = sql.removeprefix("-- ").strip()
            sql = ""
    else:
        sql, reasoning = _template_generate(tables, retry, state), ""
        if not sql:
            fallback_error = "LLM 不可用，当前问题无法通过确定性规则生成 SQL"
            logger.warning("SQL 确定性回退不可用", query=query[:80], retry=retry)

    logger.info("SQL 生成完成", sql=sql[:200], reasoning_chars=len(reasoning))

    logger.info(
        "SQL 时间补参判定边界",
        task_plan_needs_time_range=bool(contexts.routing.task_plan.get("needs_time_range", False)),
        sql_present=bool(sql.strip()),
        explanation_chars=len(explanation),
        explanation_triggered=False,
    )

    if not sql.strip() and not fallback_error:
        input_explanation = explanation.strip() or "请补充具体业务目标或目标表后重试。"
        logger.info("SQL 生成需要补充输入", explanation_chars=len(input_explanation))
        result = {
            "generated_sql": "",
            "needs_input": True,
            "input_explanation": input_explanation,
            "needs_time_range": False,
            "time_range_explanation": "",
            "retry_count": retry + 1,
            "validation_errors": [],
            "execution_error": "",
            "execution_error_type": "",
        }
        if reasoning:
            result["sql_reasoning_content"] = reasoning
        return result

    # 12.1.6 LLM 输出二次校验 — 拦截表名幻觉
    if sql.strip() and not sql.startswith("-- "):  # 跳过空回退和错误占位符
        hallucination = _check_table_hallucination(sql, tables, dialect)
        if hallucination:
            logger.warning("LLM 幻觉拦截", sql=sql[:200], unknown_tables=hallucination)
            result = {"generated_sql": sql, "retry_count": retry + 1,
                      "validation_errors": [{"type": "hallucination",
                          "message": f"SQL 引用了不存在的表: {hallucination}",
                          "unknown_tables": hallucination}]}
            if reasoning:
                result["sql_reasoning_content"] = reasoning
            logger.info("节点完成", node="generate_sql",
                       elapsed_ms=round((time.monotonic() - _start) * 1000))
            return result

    logger.info("节点完成", node="generate_sql", elapsed_ms=round((time.monotonic() - _start) * 1000))
    result = {
        "generated_sql": sql,
        "retry_count": retry + 1,
        "needs_input": False,
        "input_explanation": "",
        "needs_time_range": False,
        "time_range_explanation": "",
        "validation_errors": [],
        "explain_errors": [],
        "execution_error": "",
        "execution_error_type": "",
        "execution_retry_count": 0,
    }
    if fallback_error:
        result["execution_error"] = fallback_error
        result["execution_error_type"] = "llm_unavailable"
        logger.info(
            "SQL 生成失败边界写回",
            error_type="llm_unavailable",
            generated_sql=False,
            retry_count=result["retry_count"],
        )
    if reasoning:
        result["sql_reasoning_content"] = reasoning
    return result


# 方法作用：构造不依赖详细字段预算的完整合法表名白名单。
# Args: tables - retrieve_schema 返回的相关表结构。
# Returns: 每个合法物理表独占一行的紧凑 Prompt 文本。
def build_table_whitelist_context(tables: list[dict]) -> str:
    """白名单只包含 Schema 已确认的表名，CTE 仍可由 SQL 自行定义。"""
    names = [
        str(table.get("name", "") or "").strip()
        for table in tables
        if isinstance(table, dict) and str(table.get("name", "") or "").strip()
    ]
    unique_names = list(dict.fromkeys(names))
    if not unique_names:
        return "合法物理表白名单为空，不得猜测任何表名。"
    return "合法物理表白名单（只能使用以下物理表或 SQL 内定义的 CTE）：\n" + "\n".join(
        f"- {name}" for name in unique_names
    )


# 方法作用：按错误类型构造 SQL 重试上下文，防止幻觉 SQL 被完整回注强化。
# Args: state - 当前执行状态；retry - 当前重试序号；tables - Schema 合法表列表。
# Returns: 首次调用返回空文本；幻觉仅返回未知表和白名单，其他错误返回受限 SQL 上下文。
def build_retry_context(state: AnalysisState, retry: int, tables: list[dict]) -> str:
    """表名幻觉与普通语义错误使用不同回注策略。"""
    if retry <= 0:
        return ""
    validation_errors = state.get("validation_errors", []) or []
    hallucinations = [
        error for error in validation_errors
        if isinstance(error, dict) and error.get("type") == "hallucination"
    ]
    if hallucinations:
        unknown_tables = list(dict.fromkeys(
            str(name)
            for error in hallucinations
            for name in (error.get("unknown_tables", []) or [])
            if str(name).strip()
        ))
        return (
            f"## 第 {retry} 次生成：纠正表名幻觉\n"
            f"上一轮引用了不存在的表：{', '.join(unknown_tables) or '未知'}。\n"
            f"{build_table_whitelist_context(tables)}\n"
            "禁止复用上一轮 SQL，必须仅基于合法表名和字段 Schema 重新生成。"
        )
    previous_sql = str(state.get("generated_sql", "") or "")[:3000]
    execution_error = str(state.get("execution_error", "") or "")
    errors: list[object] = list(validation_errors)
    if execution_error:
        errors.append(execution_error)
    context = (
        f"## 第 {retry} 次重试：修正上一轮 SQL\n"
        f"失败的 SQL: {previous_sql}\n"
        f"错误原因: {json.dumps(errors, ensure_ascii=False)}\n"
        "请严格检查列名、函数和数据类型是否来自当前 Schema。"
    )
    if retry >= 2:
        context += "\n这是最后一次尝试，必须使用 Schema 中明确列出的标识符。"
    return context


# 将 state 中分散的业务证据组装为 SQL 模型可审计的上下文块。
# Args: state - 当前 LangGraph 分析状态。
# Returns: 按证据类型分段的文本；无证据时返回明确占位。
def build_sql_grounding_context(state: AnalysisState) -> str:
    """组装意图、业务规则、知识命中、枚举字典和已验证 SQL 示例。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    logger.debug(
        "组装 SQL 证据上下文入口",
        intent=contexts.routing.intent,
        rule_chars=len(state.get("business_rules_text", "") or ""),
        knowledge_chars=len(state.get("long_term_memories_text", "") or ""),
    )
    try:
        sections: list[str] = []
        intent = contexts.routing.intent
        if intent:
            sections.append(f"### 查询意图\n{intent}")
        if state.get("needs_decompose"):
            steps = state.get("decompose_steps", []) or []
            formatted_steps = [
                f"{step.get('step')}. {step.get('question', '')}"
                + (
                    f"（依赖步骤: {step.get('depends_on')}）"
                    if step.get("depends_on")
                    else ""
                )
                for step in steps
                if isinstance(step, dict) and step.get("question")
            ]
            if formatted_steps:
                sections.append(
                    "### 查询规划\n"
                    "将以下步骤合并为一条可审计的只读 SQL，优先使用 CTE 表达中间结果；"
                    "若无法在单条 SQL 中证明语义正确，则返回空 SQL 并说明原因。\n"
                    + "\n".join(formatted_steps)
                )
        business_rules = str(state.get("business_rules_text", "") or "").strip()
        if business_rules:
            sections.append(f"### 业务规则与指标口径\n{business_rules}")
        knowledge = str(state.get("long_term_memories_text", "") or "").strip()
        if knowledge:
            sections.append(f"### 知识库相关内容\n{knowledge}")
        enum_dictionary = state.get("enum_dictionary", {}) or {}
        if enum_dictionary:
            sections.append(
                "### 字段合法枚举\n" + json.dumps(
                    enum_dictionary, ensure_ascii=False, sort_keys=True,
                )
            )
        examples = state.get("few_shot_examples", []) or []
        if examples:
            formatted_examples: list[str] = []
            for index, example in enumerate(examples[:3], start=1):
                if not isinstance(example, dict):
                    continue
                question = str(example.get("question", "") or "").strip()
                sql = str(example.get("sql", "") or "").strip()
                if question and sql:
                    formatted_examples.append(
                        f"示例 {index}\n问题: {question}\nSQL: {sql}"
                    )
            if formatted_examples:
                sections.append("### 已验证 SQL 示例\n" + "\n\n".join(formatted_examples))
        result = "\n\n".join(sections) if sections else "(无额外业务证据，以 Schema 为准)"
        logger.info(
            "组装 SQL 证据上下文完成",
            section_count=len(sections),
            chars=len(result),
        )
        return result
    except Exception as exc:
        logger.error("组装 SQL 证据上下文失败", error=str(exc), exc_info=True)
        raise


async def _llm_generate(
    schema_text: str,
    dialect_hint: str,
    dialect: str,
    query: str,
    error_ctx: str,
    skill_prompt: str,
    grounding_context: str,
    config: RunnableConfig,
    conversation_history: list | None = None,
    table_whitelist: str = "",
) -> tuple[str, str, str]:
    """
    调用 LLM 流式生成 SQL 的核心逻辑，返回 (sql, reasoning_content, explanation)。

    使用 astream + config 实现真流式：
    - DeepSeek 的 reasoning_content 仅用于服务端诊断计数，不进入响应或历史
    - content token 逐 chunk 推送 → 前端实时打字机效果

    支持的响应格式（按优先级）：
      - `` ```json {"sql": "..."} ``` `` → 提取 sql 字段
      - `` ```sql ... ``` `` → 直接返回 SQL 代码块内容
      - `` ``` ... ``` `` → 尝试 JSON 解析
      - `SELECT ...;` → 正则提取第一个 SELECT 语句
      - 纯文本 → 直接返回
    """
    logger.debug(
        "LLM SQL 生成入口",
        dialect=dialect,
        query=query[:80],
        schema_chars=len(schema_text),
        grounding_chars=len(grounding_context),
    )
    # 在线程池构造同步 ChatModel，避免多源并发时阻塞事件循环。
    llm = await asyncio.to_thread(get_llm, 0)

    # 7.5.3 注入对话上下文（热/温/冷三层裁剪）
    context_text = ""
    if conversation_history:
        from src.memory.context_builder import build_llm_context
        context_text = await build_llm_context(
            conversation_history,
            query,
            node_name="generate_sql",
            model=llm,
        )

    from datetime import datetime, timezone
    _utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _current_time_functions = {
        "clickhouse": "now() / today()",
        "mysql": "NOW() / CURDATE()",
        "postgres": "CURRENT_TIMESTAMP / CURRENT_DATE",
        "sqlite": "datetime('now') / date('now')",
        "oracle": "SYSTIMESTAMP / SYSDATE",
        "mssql": "SYSUTCDATETIME() / GETDATE()",
    }
    _time_function = _current_time_functions.get(
        dialect.lower(), "CURRENT_TIMESTAMP / CURRENT_DATE",
    )
    _now_info = (
        f"UTC: {_utc}。{dialect} 当前时间函数: {_time_function}；"
        "用户指定具体日期时使用明确边界，不混用其他方言函数。"
    )
    whitelist_prompt = f"## 物理表强约束\n{table_whitelist}" if table_whitelist else ""
    prompt_sections = [
            PromptSection(
                "skill",
                skill_prompt,
                priority=75,
                max_chars=1500,
                target="system",
            ),
            PromptSection(
                "table_whitelist",
                whitelist_prompt,
                priority=110,
                min_chars=len(whitelist_prompt),
                max_chars=len(whitelist_prompt),
            ),
            PromptSection(
                "query",
                f"## 用户问题\n{query}\n请生成 SQL:",
                priority=105,
                min_chars=600,
                max_chars=2500,
            ),
            PromptSection(
                "retry_error",
                error_ctx,
                priority=90,
                min_chars=300,
                max_chars=1800,
            ),
            PromptSection(
                "schema",
                f"## 数据库表结构\n{schema_text}",
                priority=100,
                min_chars=1800,
                max_chars=_PROMPT_SCHEMA_MAX_CHARS,
            ),
            PromptSection(
                "dialect",
                f"## 方言参考\n{dialect_hint}",
                priority=90,
                min_chars=300,
                max_chars=1200,
            ),
            PromptSection(
                "current_time",
                f"## 当前时间\n{_now_info}",
                priority=85,
                min_chars=150,
                max_chars=500,
            ),
            PromptSection(
                "grounding",
                f"## 业务与知识上下文\n{grounding_context}",
                priority=60,
                min_chars=300,
                max_chars=2500,
            ),
            PromptSection(
                "history",
                f"## 对话历史\n{context_text}" if context_text else "",
                priority=30,
                max_chars=1500,
            ),
        ]

    try:
        from src.llm.invocation import prepare_invocation

        prepared = prepare_invocation(
            "sql.generate",
            prompt_sections,
            task="generate_sql",
            system_values={"dialect": dialect, "skill_instructions": ""},
            config=dict(config or {}),
            metadata={"node": "generate_sql", "dialect": dialect},
            model=llm,
        )

        # ── 流式消费：逐 chunk 累积，LangGraph 自动捕获事件推送到前端 ──
        stream_started_at = time.monotonic()
        logger.info(
            "SQL LLM 流式调用边界输入",
            model_type=type(prepared.model).__name__,
            streaming=bool(getattr(prepared.model, "streaming", False)),
            stream_chunk_timeout=getattr(prepared.model, "stream_chunk_timeout", None),
            message_count=len(prepared.messages),
            prompt_chars=sum(len(str(getattr(message, "content", "") or "")) for message in prepared.messages),
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        chunk_count = 0
        try:
            async for chunk in prepared.model.astream(
                prepared.messages,
                config=prepared.config,
            ):
                chunk_count += 1
                if chunk_count == 1:
                    logger.info(
                        "SQL LLM 首个流式块到达",
                        elapsed_ms=round((time.monotonic() - stream_started_at) * 1000),
                        chunk_type=type(chunk).__name__,
                    )
                # 提取 content（兼容 AIMessageChunk.content 和 ChatGenerationChunk.text）
                chunk_content = ""
                if hasattr(chunk, "content") and chunk.content:
                    chunk_content = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                elif hasattr(chunk, "text") and chunk.text:
                    chunk_content = chunk.text if isinstance(chunk.text, str) else str(chunk.text)
                if chunk_content:
                    content_parts.append(chunk_content)

                # 提取 reasoning_content（DeepSeek 思考链）
                reasoning = _extract_chunk_reasoning(chunk)
                if reasoning:
                    reasoning_parts.append(reasoning)
        except Exception as stream_exc:
            from langchain_openai import StreamChunkTimeoutError

            received_chunks = max(
                chunk_count,
                int(getattr(stream_exc, "chunks_received", 0) or 0),
            )
            if not isinstance(stream_exc, StreamChunkTimeoutError) or received_chunks > 0:
                raise
            logger.warning(
                "SQL LLM 流式首块超时，切换非流式回退",
                timeout_s=getattr(stream_exc, "timeout_s", None),
                chunks_received=received_chunks,
                prompt_id=prepared.prompt_id,
            )
            fallback_llm = await asyncio.to_thread(get_llm, 0, stream=False)
            response = await fallback_llm.ainvoke(
                prepared.messages,
                config=prepared.config,
            )
            fallback_content = getattr(response, "content", response)
            if fallback_content:
                content_parts.append(
                    fallback_content
                    if isinstance(fallback_content, str)
                    else str(fallback_content)
                )
            fallback_reasoning = _extract_chunk_reasoning(response)
            if fallback_reasoning:
                reasoning_parts.append(fallback_reasoning)
            logger.info(
                "SQL LLM 非流式回退完成",
                content_chars=sum(len(part) for part in content_parts),
                reasoning_chars=sum(len(part) for part in reasoning_parts),
            )

        raw = "".join(content_parts)
        reasoning_text = "".join(reasoning_parts)
        if reasoning_parts:
            logger.info(
                "LLM 推理链已收集",
                reasoning_chunks=len(reasoning_parts),
                reasoning_chars=len(reasoning_text),
            )
        logger.info("LLM 原始响应", raw=raw[:500], content_chunks=len(content_parts))

        # ── 响应内容为空时的回退 ──
        if not raw or raw.strip() == "":
            logger.error("LLM 返回空内容")
            return "-- LLM 返回空内容", reasoning_text, ""

        text = raw.strip()

        # ── 格式解析 ──
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```sql" in text:
            return text.split("```sql")[1].split("```")[0].strip(), reasoning_text, ""
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        try:
            from src.llm.output_contracts import SQLGenerationOutput, parse_json_model

            data = parse_json_model(text, SQLGenerationOutput)
            explanation = data.explanation
            sql = data.sql.strip()
            logger.info(
                "LLM SQL 生成完成",
                dialect=dialect,
                sql_chars=len(sql),
                explanation_chars=len(explanation),
            )
            return sql, reasoning_text, explanation
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug(
                "LLM SQL JSON 解析回退",
                error=str(exc),
                text_chars=len(text),
                exc_info=True,
            )

        if "SELECT" in text.upper():
            match = re.search(r"SELECT[\s\S]*?(?:;|$)", text, re.IGNORECASE)
            if match:
                return match.group(0).strip().rstrip(";"), reasoning_text, ""

        return text.strip(), reasoning_text, ""

    except Exception as e:
        logger.error("LLM 调用失败", error=str(e), exc_info=True)
        return "-- LLM 调用失败，请检查 API Key 配置", "", ""


def _extract_chunk_reasoning(chunk) -> str:
    """从流式 chunk 中提取 reasoning_content，兼容多种 LangChain 版本。

    支持 AIMessageChunk 和 ChatGenerationChunk（通过 .message 解包）。
    """
    target = chunk
    if hasattr(chunk, "message") and not hasattr(chunk, "additional_kwargs"):
        target = chunk.message

    if hasattr(target, "additional_kwargs") and isinstance(target.additional_kwargs, dict):
        r = target.additional_kwargs.get("reasoning_content", "")
        if r:
            return r if isinstance(r, str) else str(r)

    if hasattr(target, "response_metadata") and isinstance(target.response_metadata, dict):
        choices = target.response_metadata.get("choices", [])
        if choices and isinstance(choices, list):
            delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
            r = delta.get("reasoning_content", "") if isinstance(delta, dict) else ""
            if r:
                return r if isinstance(r, str) else str(r)

    if hasattr(target, "reasoning_content") and target.reasoning_content:
        rc = target.reasoning_content
        return rc if isinstance(rc, str) else str(rc)

    return ""


def _template_generate(tables: list[dict], retry: int, state: AnalysisState) -> str:
    """在无 LLM 时仅生成语义可证明的数量查询。

    Args:
        tables: 已解析的候选表结构。
        retry: 当前重试次数。
        state: LangGraph 当前状态。

    Returns:
        可证明正确的 SQL；无法确定语义时返回空字符串。
    """
    from src.graph.context import read_contexts

    query = read_contexts(state).request.user_query.strip()
    logger.debug("SQL 模板回退入口", query=query[:80], retry=retry, table_count=len(tables))
    if retry > 0 or not tables:
        logger.info("SQL 模板回退停止", reason="重试不可修复或无候选表")
        return ""

    count_keywords = ("数量", "总数", "多少", "几条", "条数", "个数", "计数", "行数", "记录数")
    is_count_query = any(keyword in query for keyword in count_keywords) or query.endswith("数")
    if is_count_query:
        table_name = tables[0].get("name", "")
        if table_name:
            sql = f"SELECT COUNT(*) AS count FROM {table_name}"
            logger.info("SQL 模板回退完成", mode="count", table=table_name)
            return sql

    logger.info("SQL 模板回退停止", reason="语义不确定")
    return ""


def _missing_time_filter(sql: str, query: str) -> bool:
    """SQL 有聚合 + 含日期列但无日期过滤 + 非累计查询 → 需要时间范围。不依赖具体表名。"""
    import re
    sql_upper = sql.upper()

    # 非聚合查询（无 SUM/COUNT/AVG/GROUP BY）不需要时间过滤
    if not re.search(r'\b(SUM|COUNT|AVG|GROUP\s+BY)\b', sql_upper):
        return False

    # SQL 中引用的列名是否包含日期特征（任何含 date/time 的列名）
    cols = set(re.findall(r'[a-z_]{3,}', sql_upper))
    date_cols = {c for c in cols if any(w in c for w in ('DATE', 'TIME'))}
    if not date_cols:
        return False

    # 日期列已在 WHERE 中做了过滤
    where_match = re.search(r'\bWHERE\b(.+?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|$)', sql_upper, re.DOTALL)
    where_clause = where_match.group(1) if where_match else ""
    if any(dc in where_clause for dc in date_cols):
        return False

    # 累计/排名/总量类不需要时间过滤
    cumulative = ("累计", "总量", "排行", "排名", "榜", "历史", "总共",
                  "全部客户", "所有客户", "每个客户", "一共有多少")
    if any(w in query for w in cumulative):
        return False

    # 用户明确指定了时间
    user_time = ("最近", "去年", "今年", "本月", "上周", "本周", "今天", "昨天",
                 "202", "全部数据", "所有时间", "不限时间", "不限制时间")
    if any(w in query for w in user_time):
        return False

    return True


# 方法作用：按真实数据库方言解析 SQL，并拦截 Schema 或 CTE 之外的表引用。
# Args: sql - 待校验 SQL；tables - Schema 已知表；dialect - 数据源真实方言。
# Returns: 未知表名列表；解析失败时返回失败关闭标记。
def _check_table_hallucination(
    sql: str,
    tables: list[dict],
    dialect: str = "",
) -> list[str]:
    """12.1.6 检查 SQL 中引用的表名是否在 relevant_tables 中存在。

    使用 sqlglot 提取 FROM/JOIN 子句中的表引用，与已知表名比对，拦截 LLM 幻觉。
    """
    if not tables:
        logger.info("表名幻觉校验跳过", reason="Schema 表为空")
        return []
    known = {t["name"].lower() for t in tables if t.get("name")}
    known_bases = {name.rsplit(".", 1)[-1] for name in known}
    unknown: list[str] = []
    parser_dialect = normalize_sql_dialect(dialect)
    logger.info(
        "表名幻觉校验边界输入",
        known_tables=sorted(known),
        parser_dialect=parser_dialect or "default",
        sql=sql,
    )
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse(sql, read=parser_dialect or None)
        logger.info(
            "表名幻觉校验解析完成",
            parsed_type=type(parsed).__name__,
            statement_count=len(parsed),
            has_walk=hasattr(parsed, "walk"),
            parser_dialect=parser_dialect or "default",
        )
        for statement in parsed:
            cte_names = {
                str(cte.alias_or_name).lower()
                for cte in statement.find_all(exp.CTE)
                if cte.alias_or_name
            }
            for table in statement.find_all(exp.Table):
                table_name = str(table.name or "").lower()
                qualified_name = ".".join(
                    str(part).lower()
                    for part in (table.catalog, table.db, table.name)
                    if part
                )
                if not table_name or table_name in cte_names:
                    continue
                if table_name in known_bases or qualified_name in known:
                    continue
                if table_name not in unknown:
                    unknown.append(table_name)
    except Exception as exc:
        logger.error(
            "表名幻觉校验异常",
            error=str(exc),
            parser_dialect=parser_dialect or "default",
            known_tables=sorted(known),
            exc_info=True,
        )
        unknown.append("SQL 解析失败，已阻断执行")
    logger.info("表名幻觉校验完成", unknown_tables=unknown, unknown_count=len(unknown))
    return unknown


def format_schema_for_prompt(tables: list[dict], dialect: str = "") -> str:
    """将表结构列表格式化为 Markdown 表格，含精确列名、样本值、方言约束。

    Args:
        tables - relevant_tables 列表
        dialect - 数据源方言，用于追加引用约束
    """
    if not tables:
        return "(无可用表结构)"
    lines = []
    # 追加列名列白名单，方便 LLM 精确引用
    all_columns: list[str] = []
    for t in tables:
        for c in t.get("columns", []):
            name = c.get("name", "")
            if name and name not in all_columns:
                all_columns.append(name)

    lines.append("## 重要约束")
    lines.append(f"- 本数据源方言: {dialect or 'SQL'}")
    lines.append(f"- 所有可用列名（白名单）: {', '.join(all_columns)}")
    lines.append("- **必须使用上述列名，禁止编造或猜测任何列名**")
    lines.append("- **禁止使用不存在的函数，参考下方方言参考**")
    lines.append("")

    for t in tables:
        lines.append(f"### 表: {t['name']} — {t.get('description', '')}")
        lines.append("| 字段 | 类型 | 约束 | 说明 |")
        lines.append("|------|------|------|------|")
        for c in t.get("columns", []):
            sample = c.get("sample", "")
            comment = c.get("comment", "") or ""
            if sample:
                comment = f"{comment}（示例: {sample}）" if comment else f"示例: {sample}"
            enum_values = c.get("enum_values", []) or []
            if enum_values:
                enum_text = ", ".join(str(value) for value in enum_values[:20])
                comment = f"{comment}；枚举: {enum_text}" if comment else f"枚举: {enum_text}"
            constraints: list[str] = []
            if c.get("is_primary_key"):
                constraints.append("PK")
            elif c.get("is_indexed"):
                constraints.append("INDEX")
            constraints.append("NULL" if c.get("is_nullable", True) else "NOT NULL")
            lines.append(
                f"| {c['name']} | {c['type']} | {', '.join(constraints)} | {comment} |"
            )
        relations = t.get("relations", []) or []
        if relations:
            lines.append("\n关联关系（只能基于这些关系 JOIN）:")
            for relation in relations:
                lines.append(
                    f"- {relation.get('relation_type', '')}: {t['name']} -> "
                    f"{relation.get('target_table', '')} ON {relation.get('join_key', '')}"
                )
        # 追加行数提示
        row_est = t.get("row_count_estimate") or t.get("row_estimate")
        if row_est:
            lines.append(f"\n（约 {row_est} 行）")
        partition_key = t.get("partition_key", "")
        if partition_key:
            lines.append(f"分区键: {partition_key}")
        lines.append("")
    return "\n".join(lines)


# 方法作用：把文本切成可用于字段匹配的中英文标识符片段。
# Args: value - 表名、列名或业务说明文本。
# Returns: 去重后的低成本匹配片段集合。
def _schema_match_tokens(value: Any) -> set[str]:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text))
    for token in list(tokens):
        if re.fullmatch(r"[a-z0-9]+", token):
            tokens.update(part for part in token.split("_") if part)
    return tokens


# 方法作用：提取列的物理名称和业务别名，供问题相关性评分复用。
# Args: column - Schema 中的列字典。
# Returns: 小写别名集合。
def _column_aliases(column: dict[str, Any]) -> set[str]:
    aliases: list[Any] = [
        column.get("name", ""),
        column.get("comment", ""),
        column.get("description", ""),
        column.get("business_name", ""),
    ]
    raw_aliases = column.get("aliases", []) or []
    aliases.extend(raw_aliases if isinstance(raw_aliases, (list, tuple, set)) else [raw_aliases])
    return {
        str(alias).strip().lower()
        for alias in aliases
        if str(alias or "").strip()
    }


# 方法作用：根据中英文业务词汇判断列是否与用户问题相关。
# Args: query - 用户原始问题；column - 待评估列；table_name - 所属表名。
# Returns: 大于零表示应优先保留，数值越大相关性越强。
def _column_match_score(query: str, column: dict[str, Any], table_name: str = "") -> int:
    query_text = str(query or "").strip().lower()
    if not query_text:
        return 0
    aliases = _column_aliases(column)
    score = 0
    for alias in aliases:
        if any(ord(char) > 127 for char in alias):
            if alias in query_text:
                score += 6
        else:
            alias_tokens = _schema_match_tokens(alias)
            query_tokens = _schema_match_tokens(query_text)
            if alias in query_tokens:
                score += 8
            score += min(3, len(alias_tokens & query_tokens))
    # 常见中文业务词与物理列名之间没有显式注释时，使用受控同义词兜底。
    name_text = " ".join(aliases)
    name_tokens = _schema_match_tokens(name_text)
    table_tokens = _schema_match_tokens(table_name)
    for chinese, english_terms in _COLUMN_SYNONYMS.items():
        matched_terms = name_tokens.intersection(english_terms)
        # orders.order_status 中的 order 只是表级公共前缀，不能让所有列都被判为相关。
        if chinese in query_text and matched_terms.difference(table_tokens):
            score += 4
    return score


# 方法作用：从索引、主键和关系表达式中提取必须保留的列名。
# Args: table - 单张表的授权 Schema 字典。
# Returns: 小写物理列名集合。
def _required_schema_columns(table: dict[str, Any]) -> set[str]:
    required: set[str] = set()
    columns = [
        column for column in table.get("columns", []) or []
        if isinstance(column, dict)
    ]
    known = {
        str(column.get("name", "") or "").strip().lower()
        for column in columns
        if str(column.get("name", "") or "").strip()
    }
    for column in columns:
        name = str(column.get("name", "") or "").strip().lower()
        if name and (column.get("is_primary_key") or column.get("is_indexed")):
            required.add(name)
    for index in table.get("indexes", []) or []:
        if not isinstance(index, dict):
            continue
        index_columns = index.get("columns", []) or []
        if isinstance(index_columns, str):
            index_columns = [index_columns]
        required.update(
            str(name).strip().lower()
            for name in index_columns
            if str(name or "").strip()
        )
    relation_texts = [
        str(relation.get("join_key", "") or "")
        for relation in table.get("relations", []) or []
        if isinstance(relation, dict)
    ]
    relation_texts.append(str(table.get("partition_key", "") or ""))
    for relation_text in relation_texts:
        for identifier in re.findall(r"(?:[A-Za-z_][\w$]*\.)?([A-Za-z_][\w$]*)", relation_text):
            normalized = identifier.strip().lower()
            if normalized in known:
                required.add(normalized)
    return required


# 方法作用：判断列名是否明显属于内部备注或审计字段。
# Args: column - Schema 中的列字典。
# Returns: 属于低价值内部字段时返回 True。
def _is_internal_schema_column(column: dict[str, Any]) -> bool:
    tokens = _schema_match_tokens(column.get("name", ""))
    return bool(tokens & _INTERNAL_COLUMN_MARKERS)


# 方法作用：复制并按问题相关性裁剪单张表的列，避免修改授权 Schema。
# Args: table - 完整授权表；query - 用户问题。
# Returns: 可安全放入 Prompt 的独立表字典。
def _prune_prompt_table_columns(table: dict[str, Any], query: str) -> dict[str, Any]:
    copied = copy.deepcopy(table)
    columns = [
        column for column in copied.get("columns", []) or []
        if isinstance(column, dict) and str(column.get("name", "") or "").strip()
    ]
    if not columns:
        copied["columns"] = []
        return copied
    table_name = str(copied.get("name", "") or "")
    required = _required_schema_columns(copied)
    scored = [
        (_column_match_score(query, column, table_name), index, column)
        for index, column in enumerate(columns)
    ]
    relevant = {
        str(column.get("name", "") or "").strip().lower()
        for score, _, column in scored
        if score > 0
    }
    if relevant:
        selected_names = required | relevant
        # 相关列过多时仍保留稳定的高分顺序，防止大表重新撑爆 Prompt。
        ranked_optional = sorted(
            (
                (
                    score,
                    -index,
                    str(column.get("name", "") or "").strip().lower(),
                )
                for score, index, column in scored
                if str(column.get("name", "") or "").strip().lower() not in required
            ),
            reverse=True,
        )
        if len(selected_names) > 32:
            selected_names = required | {
                name for _, _, name in ranked_optional[: max(0, 32 - len(required))]
            }
    else:
        # 问题没有命中字段时保留可解释字段，明显内部备注除外；避免空 Schema 诱导模型编造列。
        visible = [
            column for column in columns
            if not _is_internal_schema_column(column)
        ]
        selected_names = required | {
            str(column.get("name", "") or "").strip().lower()
            for column in visible[:32]
        }
    copied["columns"] = [
        column for column in columns
        if str(column.get("name", "") or "").strip().lower() in selected_names
    ]
    return copied


# 方法作用：按用户问题相关性裁剪详细 Schema，同时保留至少一张可用表。
# Args: tables - 完整授权表结构；query - 用户问题；dialect - 数据库方言；max_chars - 详细 Schema 上限。
# Returns: 不超过字符预算的独立表列表；空输入返回空列表。
def select_prompt_tables(
    tables: list[dict],
    query: str,
    *,
    dialect: str = "",
    max_chars: int = _PROMPT_SCHEMA_MAX_CHARS,
) -> list[dict]:
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于零")
    if not tables:
        return []
    query_tokens = _schema_match_tokens(query)
    ranked: list[tuple[int, int, dict]] = []
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            continue
        searchable = " ".join([
            str(table.get("name", "") or ""),
            str(table.get("description", "") or ""),
            " ".join(
                " ".join(sorted(_column_aliases(column)))
                for column in table.get("columns", []) or []
                if isinstance(column, dict)
            ),
        ]).lower()
        tokens = _schema_match_tokens(searchable)
        overlap = len(query_tokens & tokens) if query_tokens else 0
        # 列级评分可以识别中文业务别名，作为表排序的补充信号。
        overlap += sum(
            min(3, _column_match_score(query, column, str(table.get("name", ""))))
            for column in table.get("columns", []) or []
            if isinstance(column, dict)
        )
        ranked.append((overlap, -index, table))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    if not ranked:
        return []

    selected: list[dict] = []
    for _, _, table in ranked:
        prompt_table = _prune_prompt_table_columns(table, query)
        candidate = selected + [prompt_table]
        rendered = format_schema_for_prompt(candidate, dialect)
        if len(rendered) <= max_chars or not selected:
            selected.append(prompt_table)
        if selected and len(rendered) > max_chars and len(selected) > 1:
            selected.pop()
            break
    logger.info(
        "详细 Schema 按表按列裁剪完成",
        query=query[:80],
        total=len(tables),
        selected=len(selected),
        selected_columns=sum(len(table.get("columns", []) or []) for table in selected),
        matched=[table.get("name", "") for _, _, table in ranked[:len(selected)]],
        max_chars=max_chars,
        rendered_chars=len(format_schema_for_prompt(selected, dialect)),
    )
    return selected
