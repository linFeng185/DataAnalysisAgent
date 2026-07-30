"""AnalysisState 的轻量上下文分组与兼容适配。"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger

logger = get_logger(__name__)


class RequestContext(BaseModel):
    """描述当前轮请求和可信调用身份。"""

    model_config = ConfigDict(extra="forbid")

    user_query: str = ""
    session_id: str = ""
    tenant_id: int = Field(default=0, ge=0)
    user_id: int = Field(default=0, ge=0)
    user_role: str = ""
    request_rate_limit_checked: bool = False


class PermissionContext(BaseModel):
    """描述 API 已校验的数据源和 Skill 授权快照。"""

    model_config = ConfigDict(extra="forbid")

    datasource_access: dict[str, dict[str, Any]] = Field(default_factory=dict)
    allowed_columns: list[str] = Field(default_factory=list)
    row_filter_sql: str = ""
    enabled_skill_ids: list[str] = Field(default_factory=list)


class RoutingContext(BaseModel):
    """描述当前轮能力路由、数据源选择和 Skill 生命周期。"""

    model_config = ConfigDict(extra="forbid")

    intent: str = ""
    task_plan: dict[str, Any] = Field(default_factory=dict)
    datasource: str = ""
    selected_datasources: list[str] = Field(default_factory=list)
    skill_activation_stage: str = ""
    skill_candidate_ids: list[str] = Field(default_factory=list)
    activated_skill_ids: list[str] = Field(default_factory=list)


class ExecutionContext(BaseModel):
    """描述当前轮 SQL 执行进度，不保存 SQL 文本或查询数据。"""

    model_config = ConfigDict(extra="forbid")

    dialect: str = ""
    table_names: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    execution_retry_count: int = Field(default=0, ge=0)
    sql_valid: bool = False
    sql_explain_checked: bool = False
    validation_error_count: int = Field(default=0, ge=0)
    explain_error_count: int = Field(default=0, ge=0)
    execution_error_type: str = ""
    row_count: int = Field(default=0, ge=0)
    truncated: bool = False


class AnalysisContexts(BaseModel):
    """节点统一读取的四组请求级上下文视图。"""

    model_config = ConfigDict(extra="forbid")

    request: RequestContext
    permission: PermissionContext
    routing: RoutingContext
    execution: ExecutionContext


def _read_value(
    state: Mapping[str, Any],
    group_name: str,
    field_name: str,
    default: Any,
) -> Any:
    """优先读取兼容扁平字段，缺失时回退分组字段。"""
    if field_name in state:
        value = state.get(field_name)
        return default if value is None else value
    group = state.get(group_name, {}) or {}
    if isinstance(group, Mapping):
        value = group.get(field_name, default)
        return default if value is None else value
    return default


def build_request_context(state: Mapping[str, Any]) -> RequestContext:
    """从兼容状态构造请求上下文。"""
    return RequestContext(
        user_query=str(_read_value(state, "request_context", "user_query", "") or ""),
        session_id=str(_read_value(state, "request_context", "session_id", "") or ""),
        tenant_id=int(_read_value(state, "request_context", "tenant_id", 0) or 0),
        user_id=int(_read_value(state, "request_context", "user_id", 0) or 0),
        user_role=str(_read_value(state, "request_context", "user_role", "") or ""),
        request_rate_limit_checked=bool(
            _read_value(state, "request_context", "request_rate_limit_checked", False)
        ),
    )


def build_permission_context(state: Mapping[str, Any]) -> PermissionContext:
    """从兼容状态构造权限上下文。"""
    return PermissionContext(
        datasource_access=dict(
            _read_value(state, "permission_context", "datasource_access", {}) or {}
        ),
        allowed_columns=list(
            _read_value(state, "permission_context", "allowed_columns", []) or []
        ),
        row_filter_sql=str(
            _read_value(state, "permission_context", "row_filter_sql", "") or ""
        ),
        enabled_skill_ids=list(
            _read_value(state, "permission_context", "enabled_skill_ids", []) or []
        ),
    )


def build_routing_context(state: Mapping[str, Any]) -> RoutingContext:
    """从兼容状态构造路由上下文。"""
    return RoutingContext(
        intent=str(_read_value(state, "routing_context", "intent", "") or ""),
        task_plan=dict(_read_value(state, "routing_context", "task_plan", {}) or {}),
        datasource=str(_read_value(state, "routing_context", "datasource", "") or ""),
        selected_datasources=list(
            _read_value(state, "routing_context", "selected_datasources", []) or []
        ),
        skill_activation_stage=str(
            _read_value(state, "routing_context", "skill_activation_stage", "") or ""
        ),
        skill_candidate_ids=list(
            _read_value(state, "routing_context", "skill_candidate_ids", []) or []
        ),
        activated_skill_ids=list(
            _read_value(state, "routing_context", "activated_skill_ids", []) or []
        ),
    )


def build_execution_context(state: Mapping[str, Any]) -> ExecutionContext:
    """从兼容状态构造不含富结果的执行上下文。"""
    existing_context = state.get("execution_context", {}) or {}
    logger.info(
        "执行上下文构建边界输入",
        has_flat_row_count="query_result_full_count" in state,
        flat_row_count=state.get("query_result_full_count"),
        grouped_row_count=existing_context.get("row_count")
        if isinstance(existing_context, Mapping) else None,
        has_flat_truncated="query_result_truncated" in state,
        flat_truncated=state.get("query_result_truncated"),
    )
    relevant_tables = state.get("relevant_tables")
    if relevant_tables is None:
        existing = state.get("execution_context", {}) or {}
        relevant_names = (
            list(existing.get("table_names", []) or [])
            if isinstance(existing, Mapping)
            else []
        )
    else:
        relevant_names = [
            str(table.get("name", ""))
            for table in (relevant_tables or [])
            if isinstance(table, Mapping) and table.get("name")
        ]
    row_count = (
        int(state.get("query_result_full_count", 0) or 0)
        if "query_result_full_count" in state
        else int(_read_value(state, "execution_context", "row_count", 0) or 0)
    )
    truncated = (
        bool(state.get("query_result_truncated", False))
        if "query_result_truncated" in state
        else bool(_read_value(state, "execution_context", "truncated", False))
    )
    result = ExecutionContext(
        dialect=str(_read_value(state, "execution_context", "dialect", "") or ""),
        table_names=relevant_names,
        retry_count=int(_read_value(state, "execution_context", "retry_count", 0) or 0),
        execution_retry_count=int(
            _read_value(state, "execution_context", "execution_retry_count", 0) or 0
        ),
        sql_valid=bool(_read_value(state, "execution_context", "sql_valid", False)),
        sql_explain_checked=bool(
            _read_value(state, "execution_context", "sql_explain_checked", False)
        ),
        validation_error_count=len(state.get("validation_errors", []) or [])
        if "validation_errors" in state
        else int(_read_value(state, "execution_context", "validation_error_count", 0) or 0),
        explain_error_count=len(state.get("explain_errors", []) or [])
        if "explain_errors" in state
        else int(_read_value(state, "execution_context", "explain_error_count", 0) or 0),
        execution_error_type=str(
            _read_value(state, "execution_context", "execution_error_type", "") or ""
        ),
        row_count=row_count,
        truncated=truncated,
    )
    logger.info(
        "执行上下文构建完成",
        dialect=result.dialect,
        table_count=len(result.table_names),
        row_count=result.row_count,
        truncated=result.truncated,
        validation_error_count=result.validation_error_count,
        explain_error_count=result.explain_error_count,
    )
    return result


def build_context_groups(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """一次性生成四组可写回 AnalysisState 的轻量上下文。"""
    return {
        "request_context": build_request_context(state).model_dump(),
        "permission_context": build_permission_context(state).model_dump(),
        "routing_context": build_routing_context(state).model_dump(),
        "execution_context": build_execution_context(state).model_dump(),
    }


def read_contexts(state: Mapping[str, Any]) -> AnalysisContexts:
    """从分组状态和迁移期扁平字段构造节点只读上下文。"""
    return AnalysisContexts(
        request=build_request_context(state),
        permission=build_permission_context(state),
        routing=build_routing_context(state),
        execution=build_execution_context(state),
    )


def with_execution_context(
    state: Mapping[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    """为节点状态增量同步附加最新执行上下文。"""
    merged = {**state, **update}
    return {
        **update,
        "execution_context": build_execution_context(merged).model_dump(),
    }
