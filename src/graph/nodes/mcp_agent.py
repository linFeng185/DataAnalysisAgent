"""MCP Agent Node，执行当前请求授权的 Skill 与 MCP 工具。"""

from __future__ import annotations

from src.graph.outcome import public_error_message
from src.graph.state import AnalysisState
from src.llm.prompt_budget import PromptSection
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：根据当前是否存在 Skill 工具解析请求级工具调用上限。
# Args: state - 当前分析状态；has_skill_tools - 当前请求是否包含 Skill 工具。
# Returns: Skill 工具使用显式预算，纯 MCP 请求使用默认上限 20。
def _resolve_tool_limit(state: AnalysisState, *, has_skill_tools: bool) -> int:
    """显式零预算保持为零，避免被默认值意外放宽。"""
    if has_skill_tools:
        return max(0, int(state.get("skill_tool_budget", 0) or 0))
    return 20


# 方法作用：为已授权工具增加请求级计数和预算阻断包装。
# Args: tool - 原始工具；counter - 共享调用计数器；limit - 请求级上限。
# Returns: 保留原输入 Schema 的 StructuredTool。
def _budget_tool(tool, counter: dict[str, int], limit: int):
    """为工具增加请求级调用计数，超过预算时在工具边界阻断。"""
    from langchain_core.tools import StructuredTool

    # 方法作用：在调用原工具前原子检查并递增共享预算。
    # Args: kwargs - Agent 根据原工具 Schema 生成的参数。
    # Returns: 原工具异步执行结果。
    async def invoke(**kwargs):
        if counter["count"] >= limit:
            logger.warning("MCP 工具调用预算耗尽", tool=tool.name, limit=limit)
            raise RuntimeError("工具调用次数已达到当前请求上限")
        counter["count"] += 1
        logger.info("MCP 工具调用", tool=tool.name, call_count=counter["count"], limit=limit)
        return await tool.ainvoke(kwargs)

    return StructuredTool.from_function(
        coroutine=invoke,
        name=tool.name,
        description=getattr(tool, "description", "") or "已授权工具",
        args_schema=(
            getattr(tool, "args_schema", None)
            or (tool.get_input_schema() if hasattr(tool, "get_input_schema") else None)
        ),
        return_direct=bool(getattr(tool, "return_direct", False)),
    )


# 方法作用：按当前身份加载授权工具并执行文件分析 Agent。
# Args: state - 当前 LangGraph 分析状态。
# Returns: 与 execute_sql/build_response 兼容的标准状态增量。
async def mcp_agent_node(state: AnalysisState) -> dict:
    """8.3.1 文件分析等场景的动态工具调用 Node。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    tenant_id = contexts.request.tenant_id
    user_id = contexts.request.user_id
    logger.info(
        "MCP Agent 入口",
        tenant_id=tenant_id,
        user_id=user_id,
        skill_tool_count=len(state.get("skill_tools", []) or []),
    )
    counter = {"count": 0}
    try:
        from src.mcp_client.client_manager import get_mcp_client_manager

        mcp_manager = get_mcp_client_manager()
        await mcp_manager.ensure_scoped_servers(tenant_id, user_id)
        mcp_tools = mcp_manager.get_all_tools(tenant_id=tenant_id, user_id=user_id)
        skill_tools = state.get("skill_tools", []) or []
        all_tools = [*skill_tools, *mcp_tools]
        logger.info(
            "MCP Agent 工具边界完成",
            tenant_id=tenant_id,
            user_id=user_id,
            mcp_tool_count=len(mcp_tools),
            skill_tool_count=len(skill_tools),
        )

        skill_prompt = state.get("skill_prompt_override", "") or ""
        prompt_sections = [
                PromptSection(
                    "query",
                    f"## 用户任务\n{contexts.request.user_query}",
                    priority=100,
                    min_chars=400,
                    max_chars=1800,
                ),
                PromptSection(
                    "skill",
                    skill_prompt,
                    priority=80,
                    min_chars=300,
                    max_chars=2500,
                    target="system",
                ),
            ]

        from src.llm.client import get_task_llm, is_task_llm_available

        if not is_task_llm_available("mcp_agent"):
            logger.warning(
                "MCP Agent 降级",
                tenant_id=tenant_id,
                user_id=user_id,
                reason="任务模型不可用",
            )
            return _mcp_standard_output(
                state,
                "当前未配置可用的文件分析模型",
                success=False,
            )

        llm = get_task_llm("mcp_agent", temperature=0, reasoning=False)
        from src.llm.invocation import prepare_invocation

        prepared = prepare_invocation(
            "mcp.agent",
            prompt_sections,
            task="mcp_agent",
            metadata={
                "node": "mcp_agent",
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
            model=llm,
        )
        llm = prepared.model
        messages = prepared.messages
        if all_tools:
            from langgraph.prebuilt import create_react_agent

            tool_limit = _resolve_tool_limit(
                state,
                has_skill_tools=bool(skill_tools),
            )
            guarded_tools = [_budget_tool(tool, counter, tool_limit) for tool in all_tools]
            agent = create_react_agent(llm, guarded_tools)
            result = await agent.ainvoke(
                {"messages": messages},
                config={
                    **prepared.config,
                    "recursion_limit": max(4, tool_limit * 2 + 2),
                },
            )
            final = result["messages"][-1] if result.get("messages") else None
            agent_text = (
                final.content
                if final is not None and hasattr(final, "content")
                else ""
            ) or ""
        else:
            response = await llm.ainvoke(messages)
            agent_text = (
                response.content
                if response is not None and hasattr(response, "content")
                else ""
            ) or ""
        try:
            from src.llm.output_contracts import TextAnswerOutput, parse_json_model

            structured = parse_json_model(agent_text, TextAnswerOutput)
            agent_text = structured.answer or agent_text
        except Exception as exc:
            logger.warning(
                "MCP Agent 结构化输出回退纯文本",
                error=str(exc),
                exc_info=True,
            )
    except Exception as exc:
        logger.error(
            "MCP Agent 失败",
            tenant_id=tenant_id,
            user_id=user_id,
            error=str(exc),
            exc_info=True,
        )
        return _mcp_standard_output(
            state,
            str(exc),
            success=False,
            tool_calls=counter["count"],
        )
    logger.info(
        "MCP Agent 完成",
        tenant_id=tenant_id,
        user_id=user_id,
        output_chars=len(agent_text),
    )
    return _mcp_standard_output(
        state,
        agent_text,
        success=True,
        tool_calls=counter["count"],
    )


# 方法作用：把 Agent 文本转换为工作流统一响应契约。
# Args: state - 当前状态；agent_text - Agent 输出；success - 是否成功。
# Returns: build_response 可直接消费的状态字典。
def _mcp_standard_output(
    state: AnalysisState,
    agent_text: str,
    success: bool,
    tool_calls: int = 0,
) -> dict:
    """标准化 MCP Agent 输出，保持 SQL 路径之外的响应结构一致。"""
    logger.info(
        "MCP Agent 输出标准化入口",
        success=success,
        output_chars=len(agent_text),
    )
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    result = {
        "final_response": {
            "success": success,
            "status": "success" if success else "failed",
            "source": "mcp_agent",
            "user_query": contexts.request.user_query,
            "sql": "",
            "data": [],
            "analysis": {
                "summary": agent_text,
                "insights": [],
                "recommended_chart_type": "table",
            },
            "chart": {"type": "table", "option": {}},
        },
        "analysis_result": {
            "summary": agent_text,
            "insights": [],
            "recommended_chart_type": "table",
        },
        "chart_config": {"type": "table", "option": {}},
        "query_result_sample": [],
        "mcp_agent_output": agent_text,
        "skill_tool_calls": tool_calls,
    }
    if not success:
        result["final_response"]["error_code"] = "MCP_AGENT_FAILED"
        result["final_response"]["error_message"] = public_error_message("MCP_AGENT_FAILED")
        safe_message = public_error_message("MCP_AGENT_FAILED")
        result["final_response"]["analysis"]["summary"] = safe_message
        result["analysis_result"]["summary"] = safe_message
        result["mcp_agent_output"] = ""
    logger.info("MCP Agent 输出标准化完成", success=success)
    return result
