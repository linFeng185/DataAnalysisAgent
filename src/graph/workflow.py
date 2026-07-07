"""
LangGraph 工作流组装 — 将 9 个节点按 DAG 结构编排成完整的分析流水线。

这是整个智能体的「主控制器」。它定义了：
  1. 哪些节点参与执行（add_node × 9）
  2. 节点间的单向边（add_edge）— 固定顺序推进
  3. 节点间的条件边（add_conditional_edges）— 根据运行状态动态决定去向
  4. 5 个条件路由函数 — 每个函数读取 AnalysisState 并返回下一个节点名

图的拓扑结构：

    classify_intent (入口)
         │
         ├── mcp_agent → END       [意图为 file_analysis 时]
         │
         └── retrieve_schema       [主路径开始]
               │
              generate_sql  ←──────────────────────────────────┐
               │          ←── 语法错误 / EXPLAIN失败 / 执行错误  │
              layer3_validate                                    │
               │                                                │
               ├── security_block → build_response (终止)        │
               │                                                │
              layer4_explain                                     │
               │                                                │
              execute_sql ──────────────────────────────────────┘
               │
            analyze_result
               │
           generate_chart
               │
           build_response (终点)
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.graph.nodes.analyze_result import analyze_result_node
from src.graph.nodes.build_response import build_response_node
from src.graph.nodes.classify_intent import classify_intent_node
from src.graph.nodes.execute_sql import execute_sql_node
from src.graph.nodes.generate_chart import generate_chart_node
from src.graph.nodes.generate_sql import generate_sql_node
from src.graph.nodes.layer3_validate import layer3_validate_node
from src.graph.nodes.layer4_explain import layer4_explain_node
from src.graph.nodes.retrieve_schema import retrieve_schema_node
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 8.3 MCP Agent Node
async def mcp_agent_node(state: AnalysisState) -> dict:
    """8.3.1 文件分析等场景的动态工具调用 Node。"""
    try:
        from src.mcp.client_manager import get_mcp_client_manager
        mcp_tools = get_mcp_client_manager().get_all_tools()
        skill_tools = state.get("skill_tools", [])
        all_tools = list(skill_tools) + list(mcp_tools)

        system_prompt = state.get("skill_prompt_override", "") or \
            "你是数据分析助手，可访问文件系统和外部知识库。"

        from langchain_core.messages import HumanMessage, SystemMessage
        from src.llm.client import get_llm, is_llm_available
        if not is_llm_available():
            return {"final_response": {"success": False, "error_code": "LLM_UNAVAILABLE"}}

        llm = get_llm(temperature=0)
        messages = [SystemMessage(content=system_prompt),
                    HumanMessage(content=state.get("user_query", ""))]

        if all_tools:
            from langgraph.prebuilt import create_react_agent
            agent = create_react_agent(llm, all_tools)
            result = await agent.ainvoke({"messages": messages})
            final = result["messages"][-1] if result.get("messages") else None
            return {"final_response": {"success": True, "agent_response": getattr(final, 'content', '') or ''}}
        resp = await llm.ainvoke(messages)
        return {"final_response": {"success": True, "agent_response": resp.content or ''}}
    except Exception as e:
        logger.error("MCP Agent 失败", error=str(e))
        return {"final_response": {"success": False, "error_message": str(e)}}


# ================================================================
# 条件路由函数 — 读取 state，返回下一个节点名的字符串
# ================================================================


def after_layer3(state: AnalysisState) -> str:
    """
    SQL 层3校验后的条件路由。

    三种可能去向：
      - security_block（DDL/DML/危险函数）→ 直接终止，不进入数据库
      - syntax_error（语法错误）→ 回到 generate_sql 重试
      - 校验通过 → 继续 layer4_explain
    """
    for e in state.get("validation_errors", []):
        if e.get("type") == "security_block":
            return "build_response"  # 安全拦截是终局的，不重试
    if state.get("validation_errors"):
        return "generate_sql"  # 语法错误可重试
    return "layer4_explain"


def after_layer4(state: AnalysisState) -> str:
    """
    EXPLAIN（模拟执行）后的条件路由。

    去向：
      - 有 EXPLAIN 错误且重试次数 < 3 → generate_sql 重写 SQL
      - 有错误但次数耗尽 → build_response 终止
      - 无错误 → 进入实际执行
    """
    if state.get("explain_errors"):
        from src.config import get_settings
        return "generate_sql" if state.get("retry_count", 0) < get_settings().max_retry_count else "build_response"
    return "execute_sql"


def should_retry(state: AnalysisState) -> str:
    """
    数据库执行后的条件路由。

    三类错误处理策略：
      1. 无错误 → build_response（正常路径）
      2. 配置类错误（"未配置"、"未找到"）→ build_response（不重试）
      3. 瞬态错误（超时、连接等）→ generate_sql 重试（最多 3 次）
    """
    err = state.get("execution_error", "")
    retry = state.get("retry_count", 0)
    if not err:
        return "analyze_result"
    # 配置/资源缺失错误是永久性的，重试无意义
    if "未配置" in err or "未找到" in err or "not found" in err.lower():
        return "build_response"
    from src.config import get_settings
    if retry < get_settings().max_retry_count:
        return "generate_sql"
    return "build_response"


def route_by_intent(state: AnalysisState) -> str:
    """意图路由：多源→并行, file_analysis→MCP, metadata/chat→LLM, 其他→SQL。"""
    sources = state.get("selected_datasources", []) or []
    if len(sources) > 1:
        return "multi_source_dispatch"
    intent = state.get("intent", "")
    if intent == "file_analysis":
        return "mcp_agent"
    if intent in ("metadata", "chat"):
        return "llm_direct_answer"
    return "retrieve_schema"


# ================================================================
# StateGraph 组装 — 注册所有节点 + 连线 + 编译
# ================================================================


async def build_workflow() -> StateGraph:
    """创建并编译完整的分析流水线图。"""

    # Step 1: 创建图，指定状态类型
    workflow = StateGraph(AnalysisState)

    # Step 2: 注册 9 个执行节点（每个节点是一个 async 函数，返回 dict 合并回 state）
    workflow.add_node("classify_intent", classify_intent_node)
    workflow.add_node("retrieve_schema", retrieve_schema_node)
    workflow.add_node("generate_sql", generate_sql_node)
    workflow.add_node("layer3_validate", layer3_validate_node)
    workflow.add_node("layer4_explain", layer4_explain_node)
    workflow.add_node("execute_sql", execute_sql_node)
    workflow.add_node("analyze_result", analyze_result_node)
    workflow.add_node("generate_chart", generate_chart_node)
    workflow.add_node("build_response", build_response_node)
    workflow.add_node("mcp_agent", mcp_agent_node)
    from src.graph.nodes.llm_answer import llm_direct_answer_node
    workflow.add_node("llm_direct_answer", llm_direct_answer_node)
    from src.graph.nodes.multi_source import multi_source_dispatch_node, merge_results_node
    workflow.add_node("multi_source_dispatch", multi_source_dispatch_node)
    workflow.add_node("merge_results", merge_results_node)

    # Step 3: 设置入口节点（用户请求从这里开始）
    workflow.set_entry_point("classify_intent")

    # Step 4: 连线 — 固定边（add_edge）+ 条件边（add_conditional_edges）

    # 意图路由：按意图分叉 → 主路径或 MCP 文件分析路径
    workflow.add_conditional_edges(
        "classify_intent", route_by_intent,
        {"retrieve_schema": "retrieve_schema", "mcp_agent": END,
         "llm_direct_answer": "llm_direct_answer",
         "multi_source_dispatch": "multi_source_dispatch"}
    )
    workflow.add_edge("llm_direct_answer", END)
    workflow.add_edge("multi_source_dispatch", "merge_results")
    workflow.add_edge("merge_results", "build_response")

    # 固定边：Schema 检索 → SQL 生成
    workflow.add_edge("retrieve_schema", "generate_sql")
    workflow.add_edge("generate_sql", "layer3_validate")

    # 安全校验路由：通过 → EXPLAIN / 安全拦截 → 终止 / 语法错 → 重试
    workflow.add_conditional_edges(
        "layer3_validate", after_layer3,
        {"generate_sql": "generate_sql", "layer4_explain": "layer4_explain", "build_response": "build_response"}
    )

    # EXPLAIN 路由：通过 → 执行 / 错误 → 重试或终止
    workflow.add_conditional_edges(
        "layer4_explain", after_layer4,
        {"generate_sql": "generate_sql", "execute_sql": "execute_sql", "build_response": "build_response"}
    )

    # 执行路由：成功 → analyze_result / 瞬态错误 → 重试 / 配置错误 → 终止
    workflow.add_conditional_edges(
        "execute_sql", should_retry,
        {"analyze_result": "analyze_result", "generate_sql": "generate_sql", "build_response": "build_response"}
    )
    workflow.add_edge("analyze_result", "generate_chart")
    workflow.add_edge("generate_chart", "build_response")
    workflow.add_edge("build_response", END)
    workflow.add_edge("mcp_agent", END)      # MCP Agent 路径终点

    # Step 5: 编译 — 注入 Checkpointer 实现多轮对话状态持久化
    from src.memory.checkpointer import get_checkpointer
    return workflow.compile(checkpointer=await get_checkpointer())


# 模块级变量 — 由 main.py 的 lifespan 初始化
app = None


async def init_app():
    global app
    app = await build_workflow()
