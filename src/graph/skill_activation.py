"""工作流中的 Skill 两阶段激活与请求级资源预算。"""

from __future__ import annotations

from typing import Any

from src.graph.state import AnalysisState
from src.logging_config import get_logger


logger = get_logger(__name__)


# 方法作用：按请求身份、意图和真实表名完成两阶段 Skill 激活。
# Args: state - 当前分析状态；tables - Schema 阶段解析出的表名。
# Returns: 激活名称、Prompt、工具和请求级预算状态增量。
def activate_skills(state: AnalysisState, tables: list[str] | None = None) -> dict[str, Any]:
    """按当前身份、问题、意图和已解析表名激活 Skill。"""
    query = str(state.get("user_query", "") or "")
    intent = str(state.get("intent", "") or "")
    table_names = list(tables or [])
    try:
        from src.skill_manager import get_skill_manager

        manager = get_skill_manager()
        visible_tables = table_names or [
            str(table.get("name", ""))
            for table in state.get("relevant_tables", []) or []
            if isinstance(table, dict) and table.get("name")
        ]
        requested_skill_ids = list(state.get("enabled_skill_ids", []) or [])
        if requested_skill_ids:
            skills = manager.resolve_requested_skills(
                requested_skill_ids,
                tenant_id=state.get("tenant_id"),
                user_id=state.get("user_id"),
            )
        else:
            skills = manager.match_skills(
                query,
                intent,
                visible_tables,
                tenant_id=state.get("tenant_id"),
                user_id=state.get("user_id"),
            )
        result = {
            "activated_skills": [skill.name for skill in skills],
            "skill_prompt_override": manager.build_skill_prompt(skills),
            "skill_tools": manager.get_active_tools(skills),
            "skill_tool_budget": manager.get_tool_budget(skills),
        }
        logger.info(
            "Skill 激活阶段完成",
            stage="schema" if table_names else "intent",
            table_count=len(visible_tables),
            skill_count=len(skills),
            tool_count=len(result["skill_tools"]),
            tool_budget=result["skill_tool_budget"],
            explicit=bool(requested_skill_ids),
        )
        return result
    except Exception as exc:
        logger.error(
            "Skill 激活阶段失败",
            stage="schema" if table_names else "intent",
            error=str(exc),
            exc_info=True,
        )
        return {
            "activated_skills": [],
            "skill_prompt_override": "",
            "skill_tools": [],
            "skill_tool_budget": 0,
        }
