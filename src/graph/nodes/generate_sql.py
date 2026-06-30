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
from src.llm.client import get_llm, is_llm_available
from src.llm.prompts import SQL_GENERATION_SYSTEM, get_dialect_cheatsheet
from src.logging_config import get_logger

logger = get_logger(__name__)


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
    schema_text = format_schema_for_prompt(tables)
    # 获取该方言的 SQL 语法速查表
    dialect_hint = get_dialect_cheatsheet(dialect)
    # 技能系统注入的额外 Prompt（Phase 2）
    skill_prompt = state.get("skill_prompt_override", "")

    # ── 重试时的错误上下文 ──
    error_context = ""
    if retry > 0:
        prev_sql = state.get("generated_sql", "")
        errors = state.get("validation_errors", [])
        error_context = (
            "\n## 上一轮 SQL 失败，请修正\n"
            f"错误SQL: {prev_sql}\n错误: {json.dumps(errors)}"
        )
        if retry >= 2:
            error_context += "\n注意: 这是最后一次尝试，请仔细核对字段名和函数名"

    # ── 对话上下文（短期记忆） ──
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
    if is_llm_available():
        logger.info("SQL 生成: 调用 LLM", query=query[:80], dialect=dialect, retry=retry)
        sql, reasoning = await _llm_generate(
            schema_text, dialect_hint, query, error_context, skill_prompt, config, history,
        )
    else:
        sql, reasoning = _template_generate(tables, retry, state), ""

    logger.info("SQL 生成完成", sql=sql[:200], reasoning_chars=len(reasoning))

    # 12.1.6 LLM 输出二次校验 — 拦截表名幻觉
    if not sql.startswith("-- "):  # 跳过错误占位符
        hallucination = _check_table_hallucination(sql, tables)
        if hallucination:
            logger.warning("LLM 幻觉拦截", sql=sql[:200], unknown_tables=hallucination)
            result = {"generated_sql": sql, "retry_count": retry,
                      "validation_errors": [{"type": "hallucination",
                          "message": f"SQL 引用了不存在的表: {hallucination}",
                          "unknown_tables": hallucination}]}
            if reasoning:
                result["sql_reasoning_content"] = reasoning
            logger.info("节点完成", node="generate_sql",
                       elapsed_ms=round((time.monotonic() - _start) * 1000))
            return result

    logger.info("节点完成", node="generate_sql", elapsed_ms=round((time.monotonic() - _start) * 1000))
    result = {"generated_sql": sql, "retry_count": retry}
    if reasoning:
        result["sql_reasoning_content"] = reasoning
    return result


async def _llm_generate(
    schema_text: str,
    dialect_hint: str,
    query: str,
    error_ctx: str,
    skill_prompt: str,
    config: RunnableConfig,
    conversation_history: list | None = None,
) -> tuple[str, str]:
    """
    调用 LLM 流式生成 SQL 的核心逻辑，返回 (sql, reasoning_content)。

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
    llm = get_llm(temperature=0)

    system = SQL_GENERATION_SYSTEM.format(
        dialect="ClickHouse/MySQL/PostgreSQL",
        skill_instructions=skill_prompt,
    )

    # 7.5.3 注入对话上下文（热/温/冷三层裁剪）
    context_text = ""
    if conversation_history:
        from src.memory.context_builder import build_llm_context
        context_text = await build_llm_context(
            conversation_history, query, node_name="generate_sql",
        )

    _nl = "\n"
    _history_block = f"## 对话历史{_nl}{context_text}" if context_text else ""
    user_msg = f"""## 数据库表结构
{schema_text}

## 方言参考
{dialect_hint}
{error_ctx}

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
            return "-- LLM 返回空内容", reasoning_text

        text = raw.strip()

        # ── 格式解析 ──
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```sql" in text:
            return text.split("```sql")[1].split("```")[0].strip(), reasoning_text
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        try:
            data = json.loads(text)
            return data.get("sql", text), reasoning_text
        except json.JSONDecodeError:
            pass

        if "SELECT" in text.upper():
            match = re.search(r"SELECT[\s\S]*?(?:;|$)", text, re.IGNORECASE)
            if match:
                return match.group(0).strip().rstrip(";"), reasoning_text

        return text.strip(), reasoning_text

    except Exception as e:
        logger.error("LLM 调用失败", error=str(e))
        return "-- LLM 调用失败，请检查 API Key 配置", ""


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
    """模板回退：无 API Key 时的占位 SQL 生成。"""
    if retry > 0 and state.get("validation_errors"):
        return "-- [retry] fix validation errors"
    return "SELECT * FROM {table} LIMIT 100".format(
        table=tables[0]["name"] if tables else "unknown"
    )


def _check_table_hallucination(sql: str, tables: list[dict]) -> list[str]:
    """12.1.6 检查 SQL 中引用的表名是否在 relevant_tables 中存在。

    使用 sqlglot 提取 FROM/JOIN 子句中的表引用，与已知表名比对，拦截 LLM 幻觉。
    """
    if not tables:
        return []
    known = {t["name"].lower() for t in tables if t.get("name")}
    unknown = []
    try:
        import sqlglot
        for node in sqlglot.parse(sql).walk():
            if hasattr(node, 'name') and hasattr(node, 'alias_or_name'):
                table_name = str(node.alias_or_name).lower()
                if table_name and table_name not in known and table_name not in unknown:
                    unknown.append(table_name)
    except Exception:
        pass
    return unknown


def format_schema_for_prompt(tables: list[dict]) -> str:
    """将表结构列表格式化为 Markdown 表格，用于拼入 LLM Prompt。"""
    if not tables:
        return "(无可用表结构)"
    lines = []
    for t in tables:
        lines.append(f"### {t['name']} — {t.get('description', '')}")
        lines.append("| 字段 | 类型 | 说明 |")
        lines.append("|------|------|------|")
        for c in t.get("columns", []):
            lines.append(f"| {c['name']} | {c['type']} | {c.get('comment', '')} |")
        lines.append("")
    return "\n".join(lines)
