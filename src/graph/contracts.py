"""图编排使用的任务计划与多数据源执行契约。"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskCapability = Literal[
    "sql_analysis",
    "direct_answer",
    "file_analysis",
    "market_research",
    "forecast",
    "report",
    "external_action",
    "restore_previous_result",
]


class TaskPlan(BaseModel):
    """描述当前请求应进入的能力子图及其缺失参数。"""

    model_config = ConfigDict(extra="forbid")

    capability: TaskCapability = "sql_analysis"
    operation: str = Field(default="query", min_length=1, max_length=64)
    intent: str = Field(default="query", min_length=1, max_length=64)
    datasources: list[str] = Field(default_factory=list, max_length=16)
    needs_time_range: bool = False
    missing_inputs: list[str] = Field(default_factory=list, max_length=16)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class SourcePermission(BaseModel):
    """单个数据源 worker 可使用的列权限和行过滤条件。"""

    model_config = ConfigDict(extra="forbid")

    allowed_columns: list[str] = Field(default_factory=list)
    row_filter_sql: str = ""


class SourceQueryRequest(BaseModel):
    """多数据源调度器交给单源 SQL 子图的最小请求。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    datasource: str = Field(min_length=1, max_length=255)
    global_query: str = Field(min_length=1)
    selected_datasources: list[str] = Field(default_factory=list, max_length=16)
    session_id: str = ""
    tenant_id: int = Field(default=0, ge=0)
    user_id: int = Field(default=0, ge=0)
    user_role: str = ""
    request_rate_limit_checked: bool = False
    intent: str = "query"
    task_plan: dict[str, Any] = Field(default_factory=dict)
    enabled_skill_ids: list[str] = Field(default_factory=list)
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[Any] = Field(default_factory=list)
    user_preferences: dict[str, Any] = Field(default_factory=dict)
    long_term_memories_text: str = ""
    permission: SourcePermission = Field(default_factory=SourcePermission)

    @model_validator(mode="after")
    def validate_selected_datasource(self) -> SourceQueryRequest:
        """当请求声明来源集合时，当前来源必须属于该集合。"""
        if self.selected_datasources and self.datasource not in self.selected_datasources:
            raise ValueError("当前数据源不在已选数据源集合中")
        return self

    @classmethod
    def from_state(
        cls,
        datasource: str,
        state: Mapping[str, Any],
    ) -> SourceQueryRequest:
        """从扁平兼容状态提取单源 worker 所需的显式字段。"""
        from src.graph.context import read_contexts

        contexts = read_contexts(state)
        datasource_access = contexts.permission.datasource_access
        permission_data = datasource_access.get(datasource)
        if datasource_access and permission_data is None:
            raise PermissionError("无权访问数据源")
        permission_data = permission_data or {
            "allowed_columns": contexts.permission.allowed_columns,
            "row_filter_sql": contexts.permission.row_filter_sql,
        }
        selected = list(contexts.routing.selected_datasources)
        if not selected:
            selected = [datasource]
        return cls(
            datasource=datasource,
            global_query=contexts.request.user_query.strip(),
            selected_datasources=selected,
            session_id=contexts.request.session_id,
            tenant_id=contexts.request.tenant_id,
            user_id=contexts.request.user_id,
            user_role=contexts.request.user_role,
            request_rate_limit_checked=contexts.request.request_rate_limit_checked,
            intent=contexts.routing.intent or "query",
            task_plan=dict(contexts.routing.task_plan),
            enabled_skill_ids=list(contexts.permission.enabled_skill_ids),
            conversation_history=list(state.get("conversation_history", []) or []),
            messages=list(state.get("messages", []) or []),
            user_preferences=dict(state.get("user_preferences", {}) or {}),
            long_term_memories_text=str(state.get("long_term_memories_text", "") or ""),
            permission=SourcePermission(
                allowed_columns=list(permission_data.get("allowed_columns", []) or []),
                row_filter_sql=str(permission_data.get("row_filter_sql", "") or ""),
            ),
        )

    def build_scoped_query(self) -> str:
        """把全局问题转换为只允许访问当前来源的 SQL 子任务。"""
        return (
            "这是多数据源并行分析中的单源 SQL 子任务。"
            f"当前只负责数据源 `{self.datasource}`，当前 Schema 也只属于该数据源；"
            "其他已选数据源由独立 worker 查询，最终由合并节点统一比较。"
            "请只基于当前 Schema 生成回答全局问题所需的本数据源查询；"
            "多个指标按全局问题中的顺序输出，并使用跨源稳定的英文 snake_case 指标别名；"
            "不要输出数据源名称列，合并节点会统一注入 `_datasource`；"
            "不要尝试跨库查询，也不要因为缺少其他数据源的 Schema 而返回空 SQL。"
            f"\n全局问题：{self.global_query}"
        )

    def to_worker_state(self, *, resolved_schema: Any, dialect: str) -> dict[str, Any]:
        """构造 SQL 子图状态，禁止复制父状态中的执行产物。"""
        worker_state = {
            "user_query": self.build_scoped_query(),
            "datasource": self.datasource,
            "selected_datasources": list(self.selected_datasources),
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "user_role": self.user_role,
            "request_rate_limit_checked": self.request_rate_limit_checked,
            "intent": self.intent,
            "task_plan": dict(self.task_plan),
            "enabled_skill_ids": list(self.enabled_skill_ids),
            "conversation_history": list(self.conversation_history),
            "messages": list(self.messages),
            "user_preferences": dict(self.user_preferences),
            "long_term_memories_text": self.long_term_memories_text,
            "allowed_columns": list(self.permission.allowed_columns),
            "row_filter_sql": self.permission.row_filter_sql,
            "resolved_schema": resolved_schema,
            "dialect": dialect,
        }
        from src.graph.context import build_context_groups

        worker_state.update(build_context_groups(worker_state))
        return worker_state


class SourceQueryResult(BaseModel):
    """单源 SQL 子图的标准结果，失败时保留结构化阶段错误。"""

    model_config = ConfigDict(extra="forbid")

    datasource: str = Field(min_length=1, max_length=255)
    success: bool
    sql: str = ""
    data: list[dict[str, Any]] = Field(default_factory=list)
    dialect: str = ""
    tables: int = Field(default=0, ge=0)
    full_count: int = Field(default=0, ge=0)
    truncated: bool = False
    error: str = ""
    error_type: str = ""
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    explain_errors: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome(self) -> SourceQueryResult:
        """成功结果必须带 SQL，失败结果必须带可展示错误。"""
        if self.success and not self.sql.strip():
            raise ValueError("成功的单源结果必须包含 SQL")
        if not self.success and not self.error.strip():
            raise ValueError("失败的单源结果必须包含错误信息")
        return self

    @classmethod
    def failed(
        cls,
        datasource: str,
        error: str,
        **details: Any,
    ) -> SourceQueryResult:
        """创建结构化来源级失败结果。"""
        return cls(datasource=datasource, success=False, error=error, **details)

    def to_legacy_dict(self) -> dict[str, Any]:
        """输出当前 API 仍使用的稀疏来源结果字典。"""
        return self.model_dump(exclude_defaults=True)


class MultiSourceResult(BaseModel):
    """一次多源调度的完整来源结果集合。"""

    model_config = ConfigDict(extra="forbid")

    query: str
    selected_datasources: list[str] = Field(default_factory=list, max_length=16)
    results: list[SourceQueryResult] = Field(default_factory=list)
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> MultiSourceResult:
        """校验来源集合和统计值一致，防止部分结果被静默丢失。"""
        result_sources = [item.datasource for item in self.results]
        if len(result_sources) != len(set(result_sources)):
            raise ValueError("多数据源结果包含重复来源")
        if self.selected_datasources and set(result_sources) != set(self.selected_datasources):
            raise ValueError("多数据源结果未覆盖全部已选来源")
        expected_success = sum(1 for item in self.results if item.success)
        expected_failure = len(self.results) - expected_success
        if self.success_count != expected_success or self.failure_count != expected_failure:
            raise ValueError("多数据源结果统计与来源明细不一致")
        return self

    @classmethod
    def from_results(
        cls,
        *,
        query: str,
        selected_datasources: list[str],
        results: list[SourceQueryResult],
    ) -> MultiSourceResult:
        """根据来源明细计算不可伪造的成功和失败计数。"""
        success_count = sum(1 for item in results if item.success)
        return cls(
            query=query,
            selected_datasources=selected_datasources,
            results=results,
            success_count=success_count,
            failure_count=len(results) - success_count,
        )

    def to_legacy_results(self) -> list[dict[str, Any]]:
        """转换为现有合并与响应节点消费的结果列表。"""
        return [item.to_legacy_dict() for item in self.results]


def build_task_plan(
    intent: str,
    *,
    query: str = "",
    datasources: list[str] | None = None,
) -> TaskPlan:
    """把兼容旧意图映射为结构化任务计划。"""
    normalized_intent = str(intent or "query").strip().lower() or "query"
    normalized_query = str(query or "").strip().lower()
    if normalized_intent == "file_analysis":
        capability: TaskCapability = "file_analysis"
    elif any(marker in normalized_query for marker in ("市场研究", "行业研究", "竞品研究", "联网调研")):
        capability = "market_research"
    elif any(marker in normalized_query for marker in ("预测", "forecast")):
        capability = "forecast"
    elif (
        "分析报告" in normalized_query
        or (
            "生成" in normalized_query
            and any(marker in normalized_query for marker in ("周报", "月报", "报告"))
        )
    ):
        capability = "report"
    elif any(marker in normalized_query for marker in ("发送通知", "发送邮件", "推送到", "执行动作")):
        capability = "external_action"
    elif normalized_intent == "chat":
        capability = "direct_answer"
    elif normalized_intent == "meta":
        capability = "restore_previous_result"
    elif normalized_intent == "metadata":
        capability = "direct_answer"
    else:
        capability = "sql_analysis"

    operations = {
        "query": "query",
        "aggregation": "aggregation",
        "trend": "trend",
        "attribution": "attribution",
        "metadata": "schema_explanation",
        "chat": "conversation",
        "file_analysis": "file_analysis",
        "meta": "follow_up_analysis",
    }
    needs_time_range = normalized_intent in {"trend", "aggregation", "attribution"} and not any(
        marker in normalized_query
        for marker in (
            "最近", "近", "本月", "本周", "今年", "去年", "全部", "历史", "不限",
            "年", "月", "日", "天", "周", "季度", "两年", "三年", "五年",
            "quarter", "year", "month", "week", "last",
        )
    )
    missing_inputs = ["time_range"] if needs_time_range else []
    return TaskPlan(
        capability=capability,
        operation=operations.get(normalized_intent, normalized_intent),
        intent=normalized_intent,
        datasources=[str(item) for item in (datasources or []) if str(item).strip()],
        needs_time_range=needs_time_range,
        missing_inputs=missing_inputs,
        confidence=1.0,
    )
