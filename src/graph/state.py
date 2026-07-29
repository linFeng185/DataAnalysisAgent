"""
AnalysisState — LangGraph 状态图中流转的共享状态。

这是整个流水线的「数据契约」。每个节点函数接收 AnalysisState，
返回部分字段的 dict，LangGraph 自动合并回状态中。

total=False 意味着所有字段都是可选的（Optional），节点只填充自己负责的字段。
messages 字段使用 Annotated + add_messages 实现 append-only 语义。
"""

from __future__ import annotations

from typing import Annotated, Any, Sequence

from langchain_core.messages import BaseMessage
from langgraph.channels import UntrackedValue
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AnalysisState(TypedDict, total=False):
    """所有节点通过此状态共享输入输出。

    字段按数据流顺序组织，标注了「谁写 / 谁读」。
    """

    # ── 输入层（API 传入）──────────────────────────
    user_query: str
    """用户的自然语言查询，由 routes.py 注入。—— classify_intent / generate_sql 读"""

    datasource: str
    """目标数据源名称，由 routes.py 注入。—— retrieve_schema / execute_sql 读"""

    session_id: str
    """对外会话 ID，由 API 注入并写入历史记录。—— build_response 写入响应和查询历史"""

    tenant_id: Annotated[int, UntrackedValue]
    """认证中间件注入的租户 ID，供异步图节点执行请求级资源过滤。"""

    user_id: Annotated[int, UntrackedValue]
    """认证中间件注入的用户 ID，供 Skill/MCP 私有资源过滤。"""

    user_role: Annotated[str, UntrackedValue]
    """认证中间件注入的角色，不接受用户请求体覆盖。"""

    request_rate_limit_checked: Annotated[bool, UntrackedValue]
    """API 入口已完成用户级配额计数，执行节点不得对同一请求重复计数。"""

    intent: str
    """意图分类结果（query/trend/aggregation/attribution/metadata/file_analysis/chat）。
    —— classify_intent 写，route_by_intent / analyze_result 读"""

    # ── 扩展层（技能/工具 注入，Phase 2）──────────
    enabled_skill_ids: Annotated[list[str], UntrackedValue]
    """用户在当前请求显式授权的 Skill 复合资源 ID；空列表保留自动匹配。"""

    activated_skills: Annotated[list[str], UntrackedValue]
    skill_prompt_override: Annotated[str, UntrackedValue]
    skill_tools: Annotated[list[Any], UntrackedValue]
    skill_tool_budget: Annotated[int, UntrackedValue]
    skill_tool_calls: Annotated[int, UntrackedValue]
    conversation_history: list[dict]
    previous_turn_snapshot: dict[str, Any]
    """上一轮响应完成时固化的结构化结果，只供明确的跨轮结果追问恢复。"""

    previous_result_restored: Annotated[bool, UntrackedValue]
    """当前轮是否已通过数据源校验并恢复上一轮结构化结果。"""

    selected_datasources: Annotated[list[str], UntrackedValue]
    multi_source_results: Annotated[list[dict], UntrackedValue]
    multi_source_analysis_precomputed: Annotated[bool, UntrackedValue]
    """多源合并节点是否已生成精确分析，结果展示子图据此跳过重复统计。"""
    datasource_access: Annotated[dict[str, dict[str, Any]], UntrackedValue]
    """API 完成授权后的候选数据源及各自行列权限，模型只能在这些候选中选择。"""
    allowed_columns: Annotated[list[str], UntrackedValue]
    row_filter_sql: Annotated[str, UntrackedValue]

    needs_decompose: Annotated[bool, UntrackedValue]
    """当前问题是否需要多步规划。—— decompose_query 写，generate_sql 读"""

    decompose_steps: Annotated[list[dict], UntrackedValue]
    """结构化查询步骤。—— decompose_query 写，generate_sql 读"""

    # ── Schema 层 ──────────────────────────────────
    dialect: Annotated[str, UntrackedValue]
    """数据源方言（clickhouse/mysql/postgres/oracle/mssql）。
    —— retrieve_schema 从 Registry 获取并写入，generate_sql 读"""

    resolved_schema: Annotated[Any, UntrackedValue]
    """数据源 Registry 返回的 Schema 对象。—— retrieve_schema 写"""

    relevant_tables: Annotated[list[dict], UntrackedValue]
    """轻量表结构（name/description/columns），用于拼入 LLM Prompt。
    —— retrieve_schema 写，generate_sql 读"""

    # ── Prompt 增强层（Phase 2）────────────────────
    few_shot_examples: Annotated[list[dict], UntrackedValue]
    business_rules_text: Annotated[str, UntrackedValue]
    """业务规则文本，从知识库检索后注入 Prompt。—— retrieve_schema 初始化为空"""

    enum_dictionary: Annotated[dict[str, list[str]], UntrackedValue]
    """字段合法枚举值，键优先使用 table.column。—— retrieve_schema 写，generate_sql 读"""

    user_preferences: Annotated[dict[str, Any], UntrackedValue]
    """当前用户的私有偏好，由 prepare_turn 按认证身份加载。"""

    long_term_memories_text: Annotated[str, UntrackedValue]
    """长期记忆与知识库文本，由 prepare_turn / retrieve_schema 合并后注入 Prompt。"""

    # ── SQL 生成层 ─────────────────────────────────
    generated_sql: Annotated[str, UntrackedValue]
    """LLM 生成的 SQL 语句。—— generate_sql 写，layer3_validate / execute_sql 读"""

    needs_time_range: Annotated[bool, UntrackedValue]
    """是否需要用户补充时间范围。—— generate_sql 写，build_response 读"""

    time_range_explanation: Annotated[str, UntrackedValue]
    """时间范围补充提示文本。—— generate_sql 写，build_response 读"""

    sql_reasoning_content: Annotated[str, UntrackedValue]
    """SQL 生成时的内部推理诊断；不跟踪、不写入响应、历史或会话恢复。"""

    retry_count: Annotated[int, UntrackedValue]
    """当前重试次数（0-3）。—— generate_sql 递增写，条件路由读"""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    """LangChain 消息历史（append-only），用于多轮对话。—— generate_sql / analyze_result 读写"""

    # ── SQL 校验层 ─────────────────────────────────
    sql_valid: Annotated[bool, UntrackedValue]
    """sqlglot 语法解析结果。—— layer3_validate 写"""

    validation_errors: Annotated[list[dict], UntrackedValue]
    """校验错误列表（每项包含 type+message）。—— layer3_validate 写，
    after_layer3 / build_response 读"""

    validation_warnings: Annotated[list[dict], UntrackedValue]
    """校验警告列表。—— layer3_validate 写"""

    transpiled_sql: Annotated[str, UntrackedValue]
    """方言转译后的 SQL（当前与 generated_sql 相同）。—— layer3_validate 写"""

    # ── EXPLAIN 层 ─────────────────────────────────
    explain_errors: Annotated[list[dict], UntrackedValue]
    """EXPLAIN 执行错误列表。—— layer4_explain 写，after_layer4 读"""

    # ── 执行层 ─────────────────────────────────────
    sql_explain_checked: Annotated[bool, UntrackedValue]
    """当前最终 SQL 是否已通过统一执行服务的 EXPLAIN 校验。"""

    execution_error: Annotated[str, UntrackedValue]
    """SQL 执行错误信息。—— execute_sql 写，should_retry 读"""

    execution_error_type: Annotated[str, UntrackedValue]
    """执行错误分类（transient/sql_semantic/configuration/security/rate_limit）。"""

    execution_retry_count: Annotated[int, UntrackedValue]
    """瞬态数据库错误的原 SQL 重试次数，不与 SQL 重新生成次数混用。"""

    query_result_sample: Annotated[list[dict], UntrackedValue]
    """查询结果前 200 行（list[dict]）。—— execute_sql 写，analyze_result / build_response 读"""

    query_result_full_count: Annotated[int, UntrackedValue]
    """实际返回的结果行数。—— execute_sql 写"""

    query_result_truncated: Annotated[bool, UntrackedValue]
    """查询结果是否因 MAX_RESULT_ROWS 限制而截断。—— execute_sql 写，build_response 读"""

    query_result_statistics: Annotated[dict, UntrackedValue]
    """查询结果的基本统计（行数/数值列名）。—— execute_sql 写（当前未填充），analyze_result 读"""

    # ── 分析层 ─────────────────────────────────────
    analysis_result: Annotated[dict, UntrackedValue]
    """分析结果（summary/insights/recommended_chart_type/follow_up_questions/statistics）。
    —— analyze_result 写，build_response 读"""

    # ── 图表层 ─────────────────────────────────────
    chart_config: Annotated[dict, UntrackedValue]
    """ECharts 图表配置（type + echarts_option）。—— generate_chart 写，build_response 读"""

    # ── MCP 集成层（Phase 2）───────────────────────
    mcp_agent_output: Annotated[str, UntrackedValue]
    """MCP 子图输出（文件分析场景）。—— 仅 file_analysis 路径使用"""

    # ── 输出层 ─────────────────────────────────────
    final_response: Annotated[dict, UntrackedValue]
    """最终 API 响应体（success/session_id/user_query/sql/data/analysis/chart）。
    —— build_response 写，routes.py 读"""
