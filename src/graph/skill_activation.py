"""工作流中的 Skill 两阶段激活与请求级资源预算。"""

from __future__ import annotations

from typing import Any, Literal

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：按请求身份、意图和真实表名完成两阶段 Skill 激活。
# Args: state - 当前分析状态；tables - Schema 阶段解析出的表名。
# Returns: 激活名称、Prompt、工具和请求级预算状态增量。
def activate_skills(
    state: AnalysisState,
    tables: list[str] | None = None,
    *,
    stage: Literal["intent", "schema"] | None = None,
) -> dict[str, Any]:
    """按当前身份、问题、意图和已解析表名激活 Skill。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    query = contexts.request.user_query
    intent = contexts.routing.intent
    table_names = list(tables or [])
    activation_stage = stage or ("schema" if tables is not None else "intent")
    try:
        from src.skill_manager import get_skill_manager

        manager = get_skill_manager()
        # 意图阶段禁止读取状态中的表名，避免旧 checkpoint 或调用方残留造成提前激活。
        visible_tables = table_names if activation_stage == "schema" else []
        requested_skill_ids = list(contexts.permission.enabled_skill_ids)
        if requested_skill_ids:
            skills = manager.resolve_requested_skills(
                requested_skill_ids,
                tenant_id=contexts.request.tenant_id,
                user_id=contexts.request.user_id,
            )
            candidates = skills
        else:
            candidates = manager.get_skill_candidates(
                query,
                intent,
                tenant_id=contexts.request.tenant_id,
                user_id=contexts.request.user_id,
            ) if hasattr(manager, "get_skill_candidates") else []
            skills = manager.match_skills(
                query,
                intent,
                visible_tables,
                tenant_id=contexts.request.tenant_id,
                user_id=contexts.request.user_id,
            )
            if not candidates:
                candidates = skills
        result = {
            "activated_skills": [skill.name for skill in skills],
            "activated_skill_ids": [
                str(getattr(skill, "resource_id", "") or skill.name) for skill in skills
            ],
            "skill_candidate_ids": [
                str(getattr(skill, "resource_id", "") or skill.name) for skill in candidates
            ],
            "skill_activation_stage": activation_stage,
            "skill_prompt_override": manager.build_skill_prompt(skills),
            "skill_tools": manager.get_active_tools(skills),
            "skill_tool_budget": manager.get_tool_budget(skills),
        }
        logger.info(
            "Skill 激活阶段完成",
            stage=activation_stage,
            table_count=len(visible_tables),
            candidate_count=len(candidates),
            skill_count=len(skills),
            tool_count=len(result["skill_tools"]),
            tool_budget=result["skill_tool_budget"],
            explicit=bool(requested_skill_ids),
        )
        return result
    except Exception as exc:
        logger.error(
            "Skill 激活阶段失败",
            stage=activation_stage,
            error=str(exc),
            exc_info=True,
        )
        return {
            "activated_skills": list(state.get("activated_skills", []) or []),
            "activated_skill_ids": list(contexts.routing.activated_skill_ids),
            "skill_candidate_ids": list(contexts.routing.skill_candidate_ids),
            "skill_activation_stage": f"{activation_stage}_failed",
            "skill_prompt_override": str(state.get("skill_prompt_override", "") or ""),
            "skill_tools": list(state.get("skill_tools", []) or []),
            "skill_tool_budget": int(state.get("skill_tool_budget", 0) or 0),
        }
