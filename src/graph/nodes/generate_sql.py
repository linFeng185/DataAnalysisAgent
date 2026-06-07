"""
generate_sql 节点 — LLM 生成 SQL，支持错误回注重试。

这是流水线中最关键的节点。它负责：
  1. 将「表结构」+「方言参考」+「用户问题」组装成 LLM Prompt
  2. 调用 LLM 生成 SQL（通过 src/llm/client.py 工厂获取模型）
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

from src.graph.state import AnalysisState
from src.llm.client import get_llm, is_llm_available
from src.llm.prompts import SQL_GENERATION_SYSTEM, get_dialect_cheatsheet
from src.logging_config import get_logger

logger = get_logger(__name__)


async def generate_sql_node(state: AnalysisState) -> dict:
    """
    SQL 生成节点的主入口。

    读取 state 中的用户查询、表结构、方言信息，
    优先调用 LLM 生成 SQL，LLM 不可用时退回模板。

    返回: {"generated_sql": str, "retry_count": int}
    """
    tables = state.get("relevant_tables", [])
    dialect = state.get("dialect", "clickhouse")
    query = state.get("user_query", "")
    retry = state.get("retry_count", 0)

    # 表结构 → Markdown 格式（用于 Prompt）
    schema_text = format_schema_for_prompt(tables)
    # 获取该方言的 SQL 语法速查表（如 ClickHouse 的 date functions）
    dialect_hint = get_dialect_cheatsheet(dialect)
    # 技能系统注入的额外 Prompt（Phase 2）
    skill_prompt = state.get("skill_prompt_override", "")

    # ── 重试时的错误上下文：把上一轮的 SQL 和错误信息注入 Prompt ──
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

    # ── 主路径：LLM 生成 ──
    if is_llm_available():
        logger.info("SQL 生成: 调用 LLM", query=query[:80], dialect=dialect, retry=retry)
        sql = await _llm_generate(schema_text, dialect_hint, query, error_context, skill_prompt)
    else:
        # 回退路径：无 API Key 时用模板拼一个简单的 SELECT
        logger.warning("SQL 生成: LLM 不可用, 使用模板回退", tables_count=len(tables))
        sql = _template_generate(tables, retry, state)

    logger.info("SQL 生成完成", sql=sql[:200])
    # 注意：不递增 retry_count — 由路由判断是否重试，此处只透传
    return {"generated_sql": sql, "retry_count": retry}


async def _llm_generate(
    schema_text: str, dialect_hint: str, query: str, error_ctx: str, skill_prompt: str
) -> str:
    """
    调用 LLM 生成 SQL 的核心逻辑。

    1. 通过 get_llm(temperature=0) 获取 LLM 实例（model 和 provider 由 config 决定）
    2. 组装 Prompt：系统提示词 + 表结构 + 方言参考 + 错误回注 + 用户问题
    3. 解析 LLM 响应，从多种可能的格式中提取 SQL

    支持的响应格式（按优先级）：
      - `` ```json {"sql": "..."} ``` `` → 提取 sql 字段
      - `` ```sql ... ``` `` → 直接返回 SQL 代码块内容
      - `` ``` ... ``` `` → 尝试 JSON 解析
      - `SELECT ...;` → 正则提取第一个 SELECT 语句
      - 纯文本 → 直接返回
    """
    # temperature=0 确保输出确定性（SQL 生成不需要创意）
    llm = get_llm(temperature=0)

    # 系统提示词：告诉 LLM 它是 SQL 专家，定义输出格式
    system = SQL_GENERATION_SYSTEM.format(
        dialect="ClickHouse/MySQL/PostgreSQL",
        skill_instructions=skill_prompt,
    )

    # 用户消息：具体的一次请求
    user_msg = f"""## 数据库表结构
{schema_text}

## 方言参考
{dialect_hint}
{error_ctx}

## 用户问题
{query}

请生成 SQL:"""

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        # 调用 LLM（ainvoke = 等待完整响应，非流式）
        response = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user_msg)])
        raw = response.content if isinstance(response.content, str) else str(response.content)
        logger.info("LLM 原始响应", raw=raw[:500])

        # ── 响应内容为空时的回退 ──
        if not raw or raw.strip() == "":
            # DeepSeek/OpenAI 的 tool_calls 可能替代 content
            if hasattr(response, "tool_calls") and response.tool_calls:
                raw = str(response.tool_calls)
                logger.info("LLM 返回 tool_calls", raw=raw[:500])
            else:
                logger.error("LLM 返回空内容")
                return "-- LLM 返回空内容"

        text = raw.strip()

        # ── 格式解析：按优先级尝试多种提取方式 ──

        # 1. JSON 代码块 → 解析后取 "sql" 字段
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]

        # 2. SQL 代码块 → 直接返回块内 SQL
        elif "```sql" in text:
            return text.split("```sql")[1].split("```")[0].strip()

        # 3. 普通代码块 → 尝试 JSON 解析
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        # 尝试 JSON 解析（提取 "sql" 键）
        try:
            data = json.loads(text)
            return data.get("sql", text)
        except json.JSONDecodeError:
            pass

        # 4. 正则提取 SELECT 语句（作为最后手段）
        if "SELECT" in text.upper():
            match = re.search(r"SELECT[\s\S]*?(?:;|$)", text, re.IGNORECASE)
            if match:
                return match.group(0).strip().rstrip(";")

        # 5. 全部失败：返回原始文本
        return text.strip()

    except Exception as e:
        logger.error("LLM 调用失败", error=str(e))
        return "-- LLM 调用失败，请检查 API Key 配置"


def _template_generate(tables: list[dict], retry: int, state: AnalysisState) -> str:
    """
    模板回退：无 API Key 时的占位 SQL 生成。

    简单拼一个 SELECT * FROM table LIMIT 100 作为兜底。
    重试模式下返回占位注释。
    """
    if retry > 0 and state.get("validation_errors"):
        return "-- [retry] fix validation errors"
    return "SELECT * FROM {table} LIMIT 100".format(
        table=tables[0]["name"] if tables else "unknown"
    )


def format_schema_for_prompt(tables: list[dict]) -> str:
    """
    将表结构列表格式化为 Markdown 表格，用于拼入 LLM Prompt。

    输入: [{"name": "orders", "description": "订单表",
           "columns": [{"name": "id", "type": "UInt64", "comment": "主键"}, ...]}, ...]

    输出:
      ### orders — 订单表
      | 字段 | 类型 | 说明 |
      |------|------|------|
      | id   | UInt64 | 主键 |
      | ...
    """
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
