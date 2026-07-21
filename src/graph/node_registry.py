"""LangGraph 节点目录，集中维护 handler 与用户进度文案。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.logging_config import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class NodeDefinition:
    """单个工作流节点的稳定元数据。"""

    name: str
    handler: Callable[..., Any]
    progress_message: str = ""


_registry: dict[str, NodeDefinition] = {}
_defaults_loaded = False


# 方法作用：注册节点 handler 和可选进度文案。
# Args: name - 节点名；handler - 节点函数；progress_message - SSE 进度文案。
# Returns: NodeDefinition。
def register_node(
    name: str,
    handler: Callable[..., Any],
    progress_message: str = "",
) -> NodeDefinition:
    """显式注册节点，重复名称由最新定义覆盖。"""
    normalized = name.strip()
    logger.debug("注册工作流节点入口", node=normalized)
    if not normalized:
        logger.error("注册工作流节点失败", reason="节点名为空")
        raise ValueError("节点名不能为空")
    definition = NodeDefinition(normalized, handler, progress_message)
    _registry[normalized] = definition
    logger.info("注册工作流节点完成", node=normalized, has_progress=bool(progress_message))
    return definition


# 方法作用：导入并注册项目内置工作流节点。
# Args: 无。
# Returns: 无返回值。
def _load_default_nodes() -> None:
    """节点清单显式可审计，条件边仍由 workflow.py 定义。"""
    global _defaults_loaded
    logger.debug("加载默认工作流节点入口", already_loaded=_defaults_loaded)
    if _defaults_loaded:
        logger.info("加载默认工作流节点跳过", reason="已加载")
        return
    _defaults_loaded = True
    try:
        from src.graph.nodes.analyze_result import analyze_result_node
        from src.graph.nodes.build_response import build_response_node
        from src.graph.nodes.classify_intent import classify_intent_node
        from src.graph.nodes.decompose_query import decompose_query_node
        from src.graph.nodes.execute_sql import execute_sql_node
        from src.graph.nodes.generate_chart import generate_chart_node
        from src.graph.nodes.generate_sql import generate_sql_node
        from src.graph.nodes.layer3_validate import layer3_validate_node
        from src.graph.nodes.layer4_explain import layer4_explain_node
        from src.graph.nodes.llm_answer import llm_direct_answer_node
        from src.graph.nodes.mcp_agent import mcp_agent_node
        from src.graph.nodes.multi_source import merge_results_node, multi_source_dispatch_node
        from src.graph.nodes.prepare_turn import prepare_turn_node
        from src.graph.nodes.restore_previous_result import restore_previous_result_node
        from src.graph.nodes.retrieve_schema import retrieve_schema_node

        definitions = [
            ("prepare_turn", prepare_turn_node, ""),
            ("restore_previous_result", restore_previous_result_node, ""),
            ("classify_intent", classify_intent_node, "正在分析问题意图..."),
            ("retrieve_schema", retrieve_schema_node, "正在检索数据库表结构..."),
            ("decompose_query", decompose_query_node, "正在规划查询步骤..."),
            ("generate_sql", generate_sql_node, "正在生成 SQL 查询..."),
            ("layer3_validate", layer3_validate_node, "正在校验 SQL 安全性..."),
            ("layer4_explain", layer4_explain_node, "正在模拟执行 SQL..."),
            ("execute_sql", execute_sql_node, "正在执行查询..."),
            ("analyze_result", analyze_result_node, "正在分析查询结果..."),
            ("generate_chart", generate_chart_node, "正在生成图表..."),
            ("build_response", build_response_node, "正在组装响应..."),
            ("mcp_agent", mcp_agent_node, "正在分析文件与工具结果..."),
            ("llm_direct_answer", llm_direct_answer_node, "正在整理回答..."),
            ("multi_source_dispatch", multi_source_dispatch_node, "正在查询多个数据源..."),
            ("merge_results", merge_results_node, "正在合并多源结果..."),
        ]
        for name, handler, progress_message in definitions:
            register_node(name, handler, progress_message)
    except Exception as exc:
        _defaults_loaded = False
        logger.error("加载默认工作流节点失败", error=str(exc), exc_info=True)
        raise
    logger.info("加载默认工作流节点完成", node_count=len(_registry))


# 方法作用：返回全部节点定义。
# Args: 无。
# Returns: 按注册顺序排列的 NodeDefinition 列表。
def get_node_definitions() -> list[NodeDefinition]:
    """供 workflow.py 组装 StateGraph。"""
    _load_default_nodes()
    result = list(_registry.values())
    logger.info("获取工作流节点定义完成", node_count=len(result))
    return result


# 方法作用：生成需要推送 SSE 的节点进度映射。
# Args: 无。
# Returns: node 到中文进度文案的字典。
def get_progress_map() -> dict[str, str]:
    """过滤未声明进度文案的内部节点。"""
    _load_default_nodes()
    result = {
        definition.name: definition.progress_message
        for definition in _registry.values()
        if definition.progress_message
    }
    logger.info("获取节点进度映射完成", node_count=len(result))
    return result
