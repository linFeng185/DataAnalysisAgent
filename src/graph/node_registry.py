"""LangGraph 节点目录，集中维护 handler 与用户进度文案。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class NodeRouting:
    """节点的路由元数据：节点数据表里"给人看、也给模型看"的那几列。

    未提供路由元数据的节点视为不可被模型选择（见 node_routing.sanitize_node_sequence）。
    """

    # 这个节点做什么。
    purpose: str = ""
    # 什么情况下该选它。
    when_to_use: str = ""
    # 产出什么，供模型判断后续是否还需要别的节点。
    produces: str = ""
    # 是否必选：安全与骨架节点为 True，模型不可移除。
    mandatory: bool = False


@dataclass(frozen=True)
class NodeDefinition:
    """单个工作流节点的稳定元数据。"""

    name: str
    handler: Callable[..., Any]
    progress_message: str = ""
    routing: NodeRouting | None = None


# 规范顺序：与静态图一致的全序。模型只选"参与哪些节点"，成序由本表决定。
# 让模型直接产出有序序列等于把拓扑正确性与安全顺序交给模型保证——它把
# execute_sql 排到 layer3_validate 前面就是安全漏洞，故顺序必须由服务端固定。
CANONICAL_ORDER: tuple[str, ...] = (
    "prepare_turn",
    "classify_intent",
    "restore_previous_result",
    "retrieve_schema",
    "decompose_query",
    "generate_sql",
    "layer3_validate",
    "layer4_explain",
    "execute_sql",
    "analyze_result",
    "supplemental_query",
    "generate_chart",
    "multi_source_dispatch",
    "merge_results",
    "llm_direct_answer",
    "forecast_subgraph",
    "report_subgraph",
    "file_analysis_subgraph",
    "market_research_subgraph",
    "mcp_agent",
    "action_subgraph",
    "save_preference",
    "build_response",
)

# 模型不可移除的骨架与安全节点：prepare_turn 负责状态初始化，build_response
# 组装最终响应；缺任何一个链路就失去意义。
MANDATORY_NODE_NAMES: frozenset[str] = frozenset({
    "prepare_turn",
    "build_response",
})

# 报表单元执行默认不开放给模型的节点：
# - supplemental_query / action_subgraph / save_preference / restore_previous_result：
#   与取数无关，且旧的自主补查回环已在报表侧显式抑制。
# - market_research / file_analysis / report / mcp_agent 能力子图：它们各自要求自己的
#   证据形态（外部可追溯引用、上传文件、嵌套报表、外部工具），与报表单元的取数目标
#   不同源。实机证据：external_environment 已被这些子图接管，把已取到的 2016 行天气
#   数据覆盖成「市场研究未获得可追溯证据」失败。
NON_ROUTABLE_NODE_NAMES: frozenset[str] = frozenset({
    "supplemental_query",
    "action_subgraph",
    "save_preference",
    "restore_previous_result",
    "market_research_subgraph",
    "file_analysis_subgraph",
    "report_subgraph",
    "mcp_agent",
    # forecast_subgraph 要求严格的等间隔时间序列，报表单元的「同比/环比/季节性」
    # 会让模型误判成预测需求：实机 sales_baseline 已取回 5000 行，仍被预测子图以
    # FORECAST_INPUT_INVALID 覆盖成失败。预测属对话侧能力，不开放给报表取数单元。
    "forecast_subgraph",
})

# 可路由节点 = 规范顺序去掉不开放的那些。
ROUTABLE_NODE_NAMES: frozenset[str] = frozenset(
    name for name in CANONICAL_ORDER if name not in NON_ROUTABLE_NODE_NAMES
)

# 名称到规范顺序名次的映射，用于按序拼接选中集合。
_CANONICAL_INDEX: dict[str, int] = {name: index for index, name in enumerate(CANONICAL_ORDER)}

_registry: dict[str, NodeDefinition] = {}
_defaults_loaded = False


# 方法作用：注册节点 handler 和可选进度文案。
# Args: name - 节点名；handler - 节点函数；progress_message - SSE 进度文案；
#       routing - 可选路由元数据，缺省表示该节点不可被模型选择。
# Returns: NodeDefinition。
def register_node(
    name: str,
    handler: Callable[..., Any],
    progress_message: str = "",
    routing: NodeRouting | None = None,
) -> NodeDefinition:
    """显式注册节点，重复名称由最新定义覆盖。"""
    normalized = name.strip()
    logger.debug("注册工作流节点入口", node=normalized)
    if not normalized:
        logger.error("注册工作流节点失败", reason="节点名为空")
        raise ValueError("节点名不能为空")
    definition = NodeDefinition(normalized, handler, progress_message, routing)
    _registry[normalized] = definition
    logger.debug(
        "注册工作流节点完成",
        node=normalized,
        has_progress=bool(progress_message),
        routable=routing is not None,
    )
    return definition


# 方法作用：读取单个节点定义，未注册时返回 None。
# Args: name - 节点名。
# Returns: NodeDefinition 或 None。
def get_node_definition(name: str) -> NodeDefinition | None:
    _load_default_nodes()
    definition = _registry.get(str(name or "").strip())
    logger.debug("读取工作流节点定义完成", node=name, found=definition is not None)
    return definition


# 方法作用：读取节点在规范顺序里的名次，用于按序渲染与成序。
# Args: name - 节点名。
# Returns: 名次整数；不在规范顺序里的节点返回一个大于所有名次的哨兵值。
def canonical_index(name: str) -> int:
    return _CANONICAL_INDEX.get(str(name or "").strip(), len(CANONICAL_ORDER))


# 方法作用：汇总所有模型不可移除的必选节点。
# Args: 无。
# Returns: 必选节点名集合 = 静态骨架 ∪ 节点数据表里声明 mandatory 的节点。
def get_mandatory_node_names() -> frozenset[str]:
    """骨架节点与安全闸门都不可被模型裁掉，两者取并集。"""
    _load_default_nodes()
    declared = {
        name
        for name, definition in _registry.items()
        if definition.routing is not None and definition.routing.mandatory
    }
    result = MANDATORY_NODE_NAMES | frozenset(declared)
    logger.debug("汇总必选节点完成", node_count=len(result))
    return result


# 节点数据表：可被模型选择的节点在此声明"做什么 / 何时选 / 产出什么"。
# 只列可路由的节点；不在表内的节点（含历史子图与偏好类）不会进入模型视野。
_NODE_ROUTING: dict[str, NodeRouting] = {
    "prepare_turn": NodeRouting(
        purpose="初始化本轮会话状态（历史、偏好、会话标识）",
        when_to_use="任何一次执行都需要，没有它后续节点拿不到会话上下文",
        produces="会话历史与用户偏好",
        mandatory=True,
    ),
    "classify_intent": NodeRouting(
        purpose="判断问题的业务意图，决定后续走取数、多源还是直答",
        when_to_use="需要按问题形态分叉时必选；单元问题已明确写出取数要求时可省",
        produces="意图标签与任务计划",
    ),
    "retrieve_schema": NodeRouting(
        purpose="检索问题相关的表结构与字段",
        when_to_use="任何需要生成 SQL 的单元都要选，它是 SQL 的输入",
        produces="候选表与字段清单",
    ),
    "decompose_query": NodeRouting(
        purpose="把复杂问题拆成有顺序的查询步骤",
        when_to_use="问题需要多个步骤或多段 SQL 时选；单一指标的简单取数可省",
        produces="分步查询计划",
    ),
    "generate_sql": NodeRouting(
        purpose="根据问题与表结构生成 SQL",
        when_to_use="需要取数时必须选",
        produces="待校验的 SQL",
    ),
    "layer3_validate": NodeRouting(
        purpose="SQL 安全与规范校验（注入、越权、只读边界）",
        when_to_use="只要执行 SQL 就必须选，且必须排在执行之前",
        produces="校验结论；不通过则阻断执行",
        mandatory=True,
    ),
    "layer4_explain": NodeRouting(
        purpose="用 EXPLAIN 模拟执行，验证 SQL 可跑通",
        when_to_use="只要执行 SQL 就必须选，且必须排在执行之前",
        produces="模拟执行结论与代价估计",
        mandatory=True,
    ),
    "execute_sql": NodeRouting(
        purpose="在数据源上真实执行 SQL 并取回结果",
        when_to_use="需要真实数据时选；纯说明类单元不需要",
        produces="查询结果行与行数统计",
    ),
    "analyze_result": NodeRouting(
        purpose="对查询结果做业务分析，产出结论与发现",
        when_to_use="取到数据后要出结论时选；只做透传不需要分析时可省",
        produces="分析摘要与发现项",
    ),
    "generate_chart": NodeRouting(
        purpose="根据结果生成图表配置",
        when_to_use="结果适合可视化时选；报表自身会决定图表方案，通常可省",
        produces="ECharts 配置",
    ),
    "multi_source_dispatch": NodeRouting(
        purpose="把一次问题扇出到多个数据源并行取数",
        when_to_use="需要跨源对账或对比（如订单与发票分属不同库）时选",
        produces="各数据源的单源结果",
    ),
    "merge_results": NodeRouting(
        purpose="合并多源结果并做跨源分析",
        when_to_use="选了 multi_source_dispatch 就必须接它，否则多源结果无人收口",
        produces="跨源合并结果与失败来源披露",
    ),
    "llm_direct_answer": NodeRouting(
        purpose="不查库，直接用模型回答说明类问题",
        when_to_use="元数据说明、数据质量陈述等不需要取数的单元选它",
        produces="文字结论",
    ),
    "forecast_subgraph": NodeRouting(
        purpose="执行预测与回测",
        when_to_use="问题明确要求预测未来走势或回测模型效果时选",
        produces="预测序列与回测指标",
    ),
    "report_subgraph": NodeRouting(
        purpose="生成一份子报表",
        when_to_use="单元本身要求产出嵌套报表时选；普通取数单元不要选",
        produces="子报表文档",
    ),
    "file_analysis_subgraph": NodeRouting(
        purpose="分析用户上传的文件",
        when_to_use="问题针对已上传文件时选",
        produces="文件分析结论",
    ),
    "market_research_subgraph": NodeRouting(
        purpose="整理市场与外部环境研究证据",
        when_to_use="问题需要外部环境、景气度等非库内数据时选",
        produces="外部研究证据",
    ),
    "mcp_agent": NodeRouting(
        purpose="调用外部 MCP 工具完成任务",
        when_to_use="问题需要库外工具能力时选",
        produces="工具调用结果",
    ),
    "build_response": NodeRouting(
        purpose="组装最终响应并收口本轮状态",
        when_to_use="任何一次执行都必须以它结尾，否则拿不到最终结果",
        produces="final_response",
        mandatory=True,
    ),
}


# 方法作用：导入并注册项目内置工作流节点。
# Args: 无。
# Returns: 无返回值。
def _load_default_nodes() -> None:
    """节点清单显式可审计，条件边仍由 workflow.py 定义。"""
    global _defaults_loaded
    logger.debug("加载默认工作流节点入口", already_loaded=_defaults_loaded)
    if _defaults_loaded:
        logger.debug("加载默认工作流节点跳过", reason="已加载")
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
        from src.graph.nodes.save_preference import save_preference_node
        from src.graph.nodes.supplemental_query import supplemental_query_node
        from src.graph.subgraphs.action import build_action_subgraph
        from src.graph.subgraphs.file_analysis import build_file_analysis_subgraph
        from src.graph.subgraphs.forecast import build_forecast_subgraph
        from src.graph.subgraphs.market_research import build_market_research_subgraph
        from src.graph.subgraphs.report import build_report_subgraph

        definitions = [
            ("prepare_turn", prepare_turn_node, ""),
            ("restore_previous_result", restore_previous_result_node, ""),
            ("save_preference", save_preference_node, "正在保存用户偏好..."),
            ("classify_intent", classify_intent_node, "正在分析问题意图..."),
            ("retrieve_schema", retrieve_schema_node, "正在检索数据库表结构..."),
            ("decompose_query", decompose_query_node, "正在规划查询步骤..."),
            ("generate_sql", generate_sql_node, "正在生成 SQL 查询..."),
            ("layer3_validate", layer3_validate_node, "正在校验 SQL 安全性..."),
            ("layer4_explain", layer4_explain_node, "正在模拟执行 SQL..."),
            ("execute_sql", execute_sql_node, "正在执行查询..."),
            ("analyze_result", analyze_result_node, "正在分析查询结果..."),
            ("supplemental_query", supplemental_query_node, "证据不足，正在补充查询..."),
            ("generate_chart", generate_chart_node, "正在生成图表..."),
            ("build_response", build_response_node, "正在组装响应..."),
            ("mcp_agent", mcp_agent_node, "正在分析文件与工具结果..."),
            ("llm_direct_answer", llm_direct_answer_node, "正在整理回答..."),
            ("multi_source_dispatch", multi_source_dispatch_node, "正在查询多个数据源..."),
            ("merge_results", merge_results_node, "正在合并多源结果..."),
            ("file_analysis_subgraph", build_file_analysis_subgraph(), "正在分析文件..."),
            ("market_research_subgraph", build_market_research_subgraph(), "正在整理市场研究证据..."),
            ("forecast_subgraph", build_forecast_subgraph(), "正在执行预测与回测..."),
            ("report_subgraph", build_report_subgraph(), "正在生成分析报告..."),
            ("action_subgraph", build_action_subgraph(), "正在校验外部动作..."),
        ]
        for name, handler, progress_message in definitions:
            # 路由元数据查表注入：不在 _NODE_ROUTING 里的节点对模型不可见。
            register_node(name, handler, progress_message, _NODE_ROUTING.get(name))
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
    logger.debug("获取工作流节点定义完成", node_count=len(result))
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
    logger.debug("获取节点进度映射完成", node_count=len(result))
    return result
