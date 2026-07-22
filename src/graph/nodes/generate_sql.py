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

import json
import re
import time

from langchain_core.runnables import RunnableConfig

from src.graph.state import AnalysisState
from src.llm.client import get_task_llm as _get_task_llm
from src.llm.client import is_task_llm_available as _is_task_llm_available
from src.llm.prompts import SQL_GENERATION_SYSTEM, get_dialect_cheatsheet
from src.logging_config import get_logger

logger = get_logger(__name__)


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
# Args: temperature - 生成温度。
# Returns: 按 generate_sql 任务策略创建的 ChatModel。
def get_llm(temperature: float = 0):
    """创建 SQL 生成模型，强制关闭推理模式以降低延迟。"""
    logger.debug("创建 SQL 任务模型入口", temperature=temperature)
    model = _get_task_llm("generate_sql", temperature=temperature, reasoning=False)
    logger.info("创建 SQL 任务模型完成")
    return model


async def generate_sql_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """
    SQL 生成节点的主入口。

    读取 state 中的用户查询、表结构、方言信息，
    优先调用 LLM 流式生成 SQL，LLM 不可用时退回模板。

    返回: {"generated_sql": str, "retry_count": int}
    """
    _start = time.monotonic()
    logger.info("节点开始", node="generate_sql")
    tables = state.get("relevant_tables", [])
    dialect = state.get("dialect", "clickhouse")
    query = state.get("user_query", "")
    retry = state.get("retry_count", 0)

    # 表结构 → Markdown 格式（用于 Prompt）
    schema_text = format_schema_for_prompt(tables, dialect)
    # 获取该方言的 SQL 语法速查表
    dialect_hint = get_dialect_cheatsheet(dialect)
    # 技能系统注入的额外 Prompt（Phase 2）
    skill_prompt = state.get("skill_prompt_override", "")
    # 业务规则、知识命中、枚举和已验证示例统一组装为证据块
    grounding_context = build_sql_grounding_context(state)

    # ── 重试时的错误上下文 ──
    error_context = ""
    if retry > 0:
        prev_sql = state.get("generated_sql", "")
        exec_err = state.get("execution_error", "")
        val_errors = state.get("validation_errors", [])
        all_errors: list[str] = list(val_errors)
        if exec_err:
            all_errors.append(exec_err)
        error_context = (
            f"\n## 第 {retry} 次重试 — 上一轮 SQL 执行失败，请修正\n"
            f"失败的 SQL: {prev_sql}\n"
            f"错误原因: {json.dumps(all_errors, ensure_ascii=False)}\n"
            "请严格检查：1) 列名是否来自 Schema 2) 函数是否存在 3) 数据类型是否匹配\n"
        )
        if retry >= 2:
            error_context += "⛔ 这是最后一次尝试，必须使用 Schema 中明确列出的列名！\n"

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

    # 检查是否需要用户提供时间范围
    # 方式1：LLM 主动返回空了
    time_keywords = ("最近一周", "最近一月", "最近一年", "最近两年", "最近三年", "最近五年",
                     "时间范围", "时间过滤", "请选择时间", "请指定时间", "选择时间范围")
    needs_time_range = (not sql or sql.strip() == "") and any(
        kw in explanation for kw in time_keywords)
    # 方式2：LLM 绕过了提示生成了无时间过滤的 SQL — 代码层硬拦截
    if not needs_time_range and sql and sql.strip():
        needs_time_range = _missing_time_filter(sql, query)

    if needs_time_range:
        logger.info("需要用户指定时间范围", query=query[:80])
        result = {"generated_sql": "",
                  "needs_time_range": True,
                  "time_range_explanation": "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年 或 全部数据）",
                  "retry_count": retry + 1,
                  "validation_errors": [],
                  "execution_error": "",
                  "execution_error_type": ""}
        if reasoning:
            result["sql_reasoning_content"] = reasoning
        logger.info("节点完成", node="generate_sql",
                   elapsed_ms=round((time.monotonic() - _start) * 1000))
        return result

    # 12.1.6 LLM 输出二次校验 — 拦截表名幻觉
    if sql.strip() and not sql.startswith("-- "):  # 跳过空回退和错误占位符
        hallucination = _check_table_hallucination(sql, tables)
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
    if reasoning:
        result["sql_reasoning_content"] = reasoning
    return result


# 将 state 中分散的业务证据组装为 SQL 模型可审计的上下文块。
# Args: state - 当前 LangGraph 分析状态。
# Returns: 按证据类型分段的文本；无证据时返回明确占位。
def build_sql_grounding_context(state: AnalysisState) -> str:
    """组装意图、业务规则、知识命中、枚举字典和已验证 SQL 示例。"""
    logger.debug(
        "组装 SQL 证据上下文入口",
        intent=state.get("intent", ""),
        rule_chars=len(state.get("business_rules_text", "") or ""),
        knowledge_chars=len(state.get("long_term_memories_text", "") or ""),
    )
    try:
        sections: list[str] = []
        intent = str(state.get("intent", "") or "")
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
) -> tuple[str, str, str]:
    """
    调用 LLM 流式生成 SQL 的核心逻辑，返回 (sql, reasoning_content, explanation)。

    使用 astream + config 实现真流式：
    - DeepSeek 的 reasoning_content 逐 chunk 推送 → 前端实时看到推理过程
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
    llm = get_llm(temperature=0)

    system = SQL_GENERATION_SYSTEM.format(
        dialect=dialect,
        skill_instructions=skill_prompt,
    )

    # 7.5.3 注入对话上下文（热/温/冷三层裁剪）
    context_text = ""
    if conversation_history:
        from src.memory.context_builder import build_llm_context
        context_text = await build_llm_context(
            conversation_history, query, node_name="generate_sql",
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
    _nl = "\n"
    _history_block = f"## 对话历史{_nl}{context_text}" if context_text else ""
    user_msg = f"""## 数据库表结构
{schema_text}

## 方言参考
{dialect_hint}
{error_ctx}

## 业务与知识上下文
{grounding_context}

## 当前时间
{_now_info}

## 用户问题
{query}
{_history_block}
请生成 SQL:"""

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [SystemMessage(content=system), HumanMessage(content=user_msg)]

        # ── 流式消费：逐 chunk 累积，LangGraph 自动捕获事件推送到前端 ──
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        async for chunk in llm.astream(messages, config=config):
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

        raw = "".join(content_parts)
        reasoning_text = "".join(reasoning_parts)
        if reasoning_parts:
            logger.info("LLM 推理链", reasoning_chunks=len(reasoning_parts),
                        reasoning=reasoning_text[:500])
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
            data = json.loads(text)
            explanation = data.get("explanation", "")
            sql = data.get("sql", text).strip()
            logger.info(
                "LLM SQL 生成完成",
                dialect=dialect,
                sql_chars=len(sql),
                explanation_chars=len(explanation),
            )
            return sql, reasoning_text, explanation
        except json.JSONDecodeError as exc:
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
    query = (state.get("user_query", "") or "").strip()
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


def _check_table_hallucination(sql: str, tables: list[dict]) -> list[str]:
    """12.1.6 检查 SQL 中引用的表名是否在 relevant_tables 中存在。

    使用 sqlglot 提取 FROM/JOIN 子句中的表引用，与已知表名比对，拦截 LLM 幻觉。
    """
    if not tables:
        logger.info("表名幻觉校验跳过", reason="Schema 表为空")
        return []
    known = {t["name"].lower() for t in tables if t.get("name")}
    known_bases = {name.rsplit(".", 1)[-1] for name in known}
    unknown: list[str] = []
    logger.info(
        "表名幻觉校验边界输入",
        known_tables=sorted(known),
        sql=sql,
    )
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse(sql)
        logger.info(
            "表名幻觉校验解析完成",
            parsed_type=type(parsed).__name__,
            statement_count=len(parsed),
            has_walk=hasattr(parsed, "walk"),
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
            parsed_input_tables=sorted(known),
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
