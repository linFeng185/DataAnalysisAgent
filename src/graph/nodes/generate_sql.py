"""4.4 generate_sql Node — LLM 生成 SQL (支持错误回注重试)。"""

from __future__ import annotations

import json

from src.graph.state import AnalysisState
from src.llm.client import get_llm, is_llm_available
from src.llm.prompts import SQL_GENERATION_SYSTEM, get_dialect_cheatsheet
from src.logging_config import get_logger

logger = get_logger(__name__)


async def generate_sql_node(state: AnalysisState) -> dict:
    """LLM 生成 SQL; 无 API Key 时退回模板。"""
    tables = state.get("relevant_tables", [])
    dialect = state.get("dialect", "clickhouse")
    query = state.get("user_query", "")
    retry = state.get("retry_count", 0)

    schema_text = format_schema_for_prompt(tables)
    dialect_hint = get_dialect_cheatsheet(dialect)
    skill_prompt = state.get("skill_prompt_override", "")

    # 错误回注
    error_context = ""
    if retry > 0:
        prev_sql = state.get("generated_sql", "")
        errors = state.get("validation_errors", [])
        error_context = f"\n## 上一轮 SQL 失败，请修正\n错误SQL: {prev_sql}\n错误: {json.dumps(errors)}"
        if retry >= 2:
            error_context += "\n注意: 这是最后一次尝试，请仔细核对字段名和函数名"

    if is_llm_available():
        logger.info("SQL 生成: 调用 LLM", query=query[:80], dialect=dialect, retry=retry)
        sql = await _llm_generate(schema_text, dialect_hint, query, error_context, skill_prompt)
    else:
        logger.warning("SQL 生成: LLM 不可用, 使用模板回退", tables_count=len(tables))
        sql = _template_generate(tables, retry, state)

    logger.info("SQL 生成完成", sql=sql[:200])
    return {"generated_sql": sql, "retry_count": retry}


async def _llm_generate(schema_text: str, dialect_hint: str, query: str, error_ctx: str, skill_prompt: str) -> str:
    """调用 LLM 生成 SQL。"""
    llm = get_llm(temperature=0)
    system = SQL_GENERATION_SYSTEM.format(dialect="ClickHouse/MySQL/PostgreSQL", skill_instructions=skill_prompt)

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
        response = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user_msg)])
        raw = response.content if isinstance(response.content, str) else str(response.content)
        logger.info("LLM 原始响应", raw=raw[:500])

        # 也可能是 AIMessage 直接包含了 tool_calls 或其他字段
        if not raw or raw.strip() == "":
            if hasattr(response, "tool_calls") and response.tool_calls:
                raw = str(response.tool_calls)
                logger.info("LLM 返回 tool_calls", raw=raw[:500])
            else:
                logger.error("LLM 返回空内容", response=str(response)[:500])
                return "-- LLM 返回空内容"

        text = raw.strip()

        # 尝试从 JSON 提取
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```sql" in text:
            return text.split("```sql")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        try:
            data = json.loads(text)
            return data.get("sql", text)
        except json.JSONDecodeError:
            pass

        # 如果没 JSON 也没代码块，直接返回
        if "SELECT" in text.upper():
            # 提取第一个 SELECT 语句
            import re
            match = re.search(r"SELECT[\s\S]*?(?:;|$)", text, re.IGNORECASE)
            if match:
                return match.group(0).strip().rstrip(";")
        return text.strip()
    except Exception as e:
        logger.error("LLM 调用失败", error=str(e))
        return "-- LLM 调用失败，请检查 API Key 配置"


def _template_generate(tables: list[dict], retry: int, state: AnalysisState) -> str:
    """Phase 1 占位: 无 API Key 时的模板生成。"""
    if retry > 0 and state.get("validation_errors"):
        return "-- [retry] fix validation errors"
    return "SELECT * FROM {table} LIMIT 100".format(
        table=tables[0]["name"] if tables else "unknown"
    )


def format_schema_for_prompt(tables: list[dict]) -> str:
    """表结构列表 → Markdown Prompt 文本。"""
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
