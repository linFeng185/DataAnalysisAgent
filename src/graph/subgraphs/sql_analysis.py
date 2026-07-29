"""复用单源主图节点和路由语义的 SQL 子图，供多源 worker 调用。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, StateGraph

from src.graph.state import AnalysisState
from src.logging_config import get_logger


logger = get_logger(__name__)
SQL_ANALYSIS_NODE_NAMES = (
    "retrieve_schema",
    "decompose_query",
    "generate_sql",
    "layer3_validate",
    "layer4_explain",
    "execute_sql",
)


# 方法作用：从节点模块读取本次构图应使用的 SQL handler，避免复用注册表中的旧函数引用。
# Args: 无。
# Returns: SQL 节点名到当前模块 handler 的映射。
def _get_sql_analysis_handlers() -> dict[str, Callable[..., Any]]:
    """每次构图都解析当前函数，使测试替身和运行时模块替换立即生效。"""
    from src.graph.nodes import decompose_query as decompose_query_module
    from src.graph.nodes import execute_sql as execute_sql_module
    from src.graph.nodes import generate_sql as generate_sql_module
    from src.graph.nodes import layer3_validate as layer3_validate_module
    from src.graph.nodes import layer4_explain as layer4_explain_module
    from src.graph.nodes import retrieve_schema as retrieve_schema_module

    return {
        "retrieve_schema": retrieve_schema_module.retrieve_schema_node,
        "decompose_query": decompose_query_module.decompose_query_node,
        "generate_sql": generate_sql_module.generate_sql_node,
        "layer3_validate": layer3_validate_module.layer3_validate_node,
        "layer4_explain": layer4_explain_module.layer4_explain_node,
        "execute_sql": execute_sql_module.execute_sql_node,
    }


# 方法作用：向任意 StateGraph 注册唯一的 SQL 节点集合和条件边拓扑。
# Args: graph - 待装配图；各 route 参数 - 主图路由函数；direct/failure/success_target - 调用方终点。
# Returns: 无返回值；节点缺失或重复由 StateGraph/注册表立即抛错。
def add_sql_analysis_flow(
    graph: StateGraph,
    *,
    after_retrieve_schema: Callable[[AnalysisState], str],
    after_generate_sql: Callable[[AnalysisState], str],
    after_layer3: Callable[[AnalysisState], str],
    after_layer4: Callable[[AnalysisState], str],
    should_retry: Callable[[AnalysisState], str],
    direct_target: Any,
    failure_target: Any,
    success_target: Any,
) -> None:
    """主图和 worker 只配置终点，SQL 节点与重试边保持单一定义。"""
    handlers = _get_sql_analysis_handlers()
    missing = [
        name for name in SQL_ANALYSIS_NODE_NAMES
        if name not in handlers
    ]
    if missing:
        raise RuntimeError(f"SQL 分析节点未注册: {', '.join(missing)}")
    for name in SQL_ANALYSIS_NODE_NAMES:
        logger.info(
            "SQL 分析节点解析",
            node=name,
            handler_module=getattr(handlers[name], "__module__", ""),
            handler_name=getattr(handlers[name], "__name__", ""),
        )
        graph.add_node(name, handlers[name])

    graph.add_conditional_edges(
        "retrieve_schema",
        after_retrieve_schema,
        {
            "decompose_query": "decompose_query",
            "llm_direct_answer": direct_target,
        },
    )
    graph.add_edge("decompose_query", "generate_sql")
    graph.add_conditional_edges(
        "generate_sql",
        after_generate_sql,
        {
            "generate_sql": "generate_sql",
            "layer3_validate": "layer3_validate",
            "build_response": failure_target,
        },
    )
    graph.add_conditional_edges(
        "layer3_validate",
        after_layer3,
        {
            "generate_sql": "generate_sql",
            "layer4_explain": "layer4_explain",
            "build_response": failure_target,
        },
    )
    graph.add_conditional_edges(
        "layer4_explain",
        after_layer4,
        {
            "generate_sql": "generate_sql",
            "execute_sql": "execute_sql",
            "build_response": failure_target,
        },
    )
    graph.add_conditional_edges(
        "execute_sql",
        should_retry,
        {
            "analyze_result": success_target,
            "execute_sql": "execute_sql",
            "generate_sql": "generate_sql",
            "build_response": failure_target,
        },
    )
    logger.info(
        "SQL 分析流程装配完成",
        node_count=len(SQL_ANALYSIS_NODE_NAMES),
        direct_target=str(direct_target),
        failure_target=str(failure_target),
        success_target=str(success_target),
    )


# 方法作用：基于主图节点和路由函数构建多源 worker 使用的完整 SQL 子图。
# Args: 无。
# Returns: 包含 Schema、规划、校验、EXPLAIN、执行和重试边的已编译子图。
def build_sql_analysis_subgraph() -> Any:
    """创建不持久化中间态的 SQL 子图。"""
    from src.graph.workflow import (
        after_generate_sql,
        after_layer3,
        after_layer4,
        after_retrieve_schema,
        should_retry,
    )
    graph = StateGraph(AnalysisState)
    add_sql_analysis_flow(
        graph,
        after_retrieve_schema=after_retrieve_schema,
        after_generate_sql=after_generate_sql,
        after_layer3=after_layer3,
        after_layer4=after_layer4,
        should_retry=should_retry,
        direct_target=END,
        failure_target=END,
        success_target=END,
    )
    graph.set_entry_point("retrieve_schema")
    compiled = graph.compile()
    logger.info("SQL 分析子图构建完成", node_count=6)
    return compiled
