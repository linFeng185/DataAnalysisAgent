"""报表单元节点路由：由模型从节点数据表里选节点，服务端按规范顺序成序。

安全边界（见 spec/50-report-node-routing.md）：模型只决定**参与哪些节点**，
顺序由 `node_registry.CANONICAL_ORDER` 固定——让模型产出有序序列等于把
"execute_sql 排在 layer3_validate 之前"这类安全漏洞交给它自觉避免。
"""

from __future__ import annotations

from typing import Any

from src.graph.node_registry import (
    CANONICAL_ORDER,
    MANDATORY_NODE_NAMES,
    ROUTABLE_NODE_NAMES,
    canonical_index,
    get_mandatory_node_names,
    get_node_definition,
    get_node_definitions,
)
from src.logging_config import get_logger

logger = get_logger(__name__)

# 提示词标识与模型任务标识：项目约定 prompt_id 用点号、task 用下划线。
_PROMPT_ID = "report.route_nodes"
_PROMPT_TASK = "report_route_nodes"
# 序列长度上限：规范顺序全展开是 23，超过它的选择一定有问题。
_MAX_SEQUENCE = 16
# 单条 SQL 流：一旦要执行 SQL，这些节点缺一不可且顺序由规范顺序保证。
# decompose_query 与 retrieve_schema 同等必要：它产出分步查询计划，漏掉会让
# 多步问题退化成单条 SQL，且孤立地留在序列里毫无意义。
_SQL_FLOW_NODES = frozenset({
    "retrieve_schema",
    "decompose_query",
    "generate_sql",
    "layer3_validate",
    "layer4_explain",
    "execute_sql",
})
# 这些节点没有 SQL 结果就没有合法输入；模型只选其中任一项也必须补齐完整 SQL 流。
_SQL_DEPENDENT_NODES = (_SQL_FLOW_NODES - {"retrieve_schema"}) | frozenset({
    "analyze_result",
    "generate_chart",
})
# 多源流：扇出必须接合并，否则多源结果无人收口。
_MULTI_SOURCE_FLOW = frozenset({"multi_source_dispatch", "merge_results"})
# 单源分支专属节点：多源分支由 merge_results 内部完成跨源分析，不需要这些。
# 漏删 analyze_result 会让它在 multi_source_dispatch **之前**执行（规范顺序里它更靠前），
# 于是在没有数据的情况下先分析一遍——实机表现为「0 行空 SQL 却判 success」的假成功。
_SINGLE_SOURCE_ONLY = _SQL_FLOW_NODES | frozenset({"analyze_result", "generate_chart"})


# 方法作用：把节点数据表渲染成提示词可读的清单。
# Args: allowed - 允许出现的节点名集合，缺省用可路由集合。
# Returns: 每行一个节点的清单文本，含用途、适用场景与产出。
def render_node_catalog(*, allowed: set[str] | None = None) -> str:
    allowed_names = set(allowed) if allowed is not None else set(ROUTABLE_NODE_NAMES)
    mandatory = get_mandatory_node_names()
    lines: list[str] = []
    # 按规范顺序渲染：模型看到的顺序就是实际执行顺序，减少误判。
    for definition in sorted(get_node_definitions(), key=lambda item: canonical_index(item.name)):
        if definition.name not in allowed_names:
            continue
        routing = definition.routing
        if routing is None:
            continue
        flag = "【必选】" if definition.name in mandatory else ""
        lines.append(
            f"- {definition.name}{flag}：{routing.purpose}"
            f"｜何时选：{routing.when_to_use}"
            f"｜产出：{routing.produces}"
        )
    text = "\n".join(lines)
    logger.debug("节点目录渲染完成", node_count=len(lines), chars=len(text))
    return text


# 方法作用：把模型给出的节点集合收敛成一条可执行的规范序列（护栏 G1~G5）。
# Args: chosen - 模型选择的节点名列表。
# Returns: 按规范顺序排列的节点序列；不足以构成一条链路时返回空列表。
def sanitize_node_sequence(chosen: list[str] | None, *, allow_multi_source: bool = False) -> list[str]:
    """纯函数，不做 IO，便于穷举单测。返回空列表表示调用方应退回静态图。

    allow_multi_source 必须由调用方按**实际授权数据源个数**给出：模型看不到这个事实，
    让它自行判断会让单源单元选多源扇出，实机表现为 0 行假成功。缺省 False 是保守值。
    """
    raw = [str(name or "").strip() for name in (chosen or [])]
    # G1 白名单：不在可路由集合里的节点一律丢弃，绝不因为模型点名就去取 handler。
    unknown = sorted({name for name in raw if name and name not in ROUTABLE_NODE_NAMES})
    selected = {name for name in raw if name in ROUTABLE_NODE_NAMES}
    if unknown:
        logger.warning("节点路由丢弃未知节点", dropped=unknown)
    # G2 无条件必选仅限入口/出口；校验节点是 SQL 路径的条件必选项。
    selected |= MANDATORY_NODE_NAMES
    # G3 SQL 流完整性：任一依赖 SQL 结果的节点被选中，都补齐生成、校验、
    # EXPLAIN 与执行，避免模型少选后在空状态上直接分析或渲染。
    if selected & _SQL_DEPENDENT_NODES:
        selected |= _SQL_FLOW_NODES
        logger.debug("节点路由补齐 SQL 流", nodes=sorted(_SQL_FLOW_NODES))
    # 调用方已确认多个来源都与本单元相关时，单源 SQL 图只会使用 datasource 主标签
    # 指向的首源，其余来源会静默遗漏。模型选了取数形态就提升为多源扇出；直答不变。
    if allow_multi_source and "execute_sql" in selected and "multi_source_dispatch" not in selected:
        selected.add("multi_source_dispatch")
        logger.warning("节点路由多源单元选择了单源 SQL，提升为多源流")
    if "multi_source_dispatch" in selected:
        selected |= _MULTI_SOURCE_FLOW
        logger.debug("节点路由补齐多源流", nodes=sorted(_MULTI_SOURCE_FLOW))
    # 单源流与多源流互斥：静态图里它们由意图二选一，同时出现会让状态互相覆盖。
    stripped_multi_source = False
    if _MULTI_SOURCE_FLOW <= selected:
        if allow_multi_source:
            # 保留多源流，并把单源专属节点整组剔除：只删 SQL 流会留下 analyze_result，
            # 而它在规范顺序里排在多源扇出**之前**，会在没有数据时先分析一遍。
            selected -= _SINGLE_SOURCE_ONLY
            selected.discard("llm_direct_answer")
            logger.warning("节点路由同时选中单源与多源，保留多源流", kept=sorted(_MULTI_SOURCE_FLOW))
        else:
            # 实际只有一个授权源：多源扇出没有意义，回退到单源取数链路。
            selected -= _MULTI_SOURCE_FLOW
            stripped_multi_source = True
            logger.warning("节点路由选中多源但实际单源，回退单源取数", kept=sorted(selected))
    # 只因"多源被剔除"而落空时补标准取数链路；模型本就什么都没选的情况不在此列——
    # 那应当退回静态图按意图分叉（可能本该是直答单元），强行灌 SQL 链路会判错形态。
    if stripped_multi_source and not [name for name in selected if name not in MANDATORY_NODE_NAMES]:
        selected |= _SQL_FLOW_NODES | {"analyze_result"}
        logger.info("节点路由剔除多源后无实质节点，补默认取数链路", nodes=sorted(_SQL_FLOW_NODES))
    # SQL、跨源与直答是互斥执行形态。SQL 或跨源证据优先于直答，避免同一状态被后写覆盖。
    if "execute_sql" in selected or _MULTI_SOURCE_FLOW <= selected:
        selected.discard("llm_direct_answer")
    # G4 首末固定。
    selected |= {"prepare_turn", "build_response"}
    sequence = [name for name in CANONICAL_ORDER if name in selected]
    complete_shape = (
        "execute_sql" in selected
        or _MULTI_SOURCE_FLOW <= selected
        or "llm_direct_answer" in selected
    )
    # 不完整选择交给静态图按意图恢复，不能把 retrieve_schema/classify 等半条链强行执行。
    if not complete_shape:
        logger.warning("节点路由未产生实质节点，退回静态图", sequence=sequence)
        return []
    # G5 长度上限：以截断中段的方式保形，末尾始终是 build_response。
    if len(sequence) > _MAX_SEQUENCE:
        middle = [name for name in sequence[: _MAX_SEQUENCE - 1] if name not in {"prepare_turn", "build_response"}]
        sequence = ["prepare_turn", *middle[: _MAX_SEQUENCE - 2], "build_response"]
        logger.warning("节点路由序列超长已截断", limit=_MAX_SEQUENCE, kept=len(sequence))
    logger.info("节点路由序列生成完成", sequence=sequence)
    return sequence


# 方法作用：请求模型从节点数据表里选出本次要走的节点。
# Args: question - 分析单元问题；catalog - 已渲染的节点清单。
# Returns: 模型决策；模型不可用或调用失败时返回 None。
async def _invoke_node_route(question: str, catalog: str) -> Any:
    from src.llm.client import is_task_llm_available

    if not is_task_llm_available(_PROMPT_TASK):
        logger.info("节点路由模型不可用", task=_PROMPT_TASK)
        return None
    from src.llm.invocation import invoke_structured
    from src.llm.output_contracts import ReportNodeRouteDecision
    from src.llm.prompt_budget import PromptSection

    return await invoke_structured(
        _PROMPT_ID,
        [
            PromptSection("catalog", f"## 可用节点\n{catalog}", priority=100, min_chars=100, max_chars=20000),
            PromptSection("question", f"## 分析单元问题\n{question}", priority=90, min_chars=1, max_chars=4000),
        ],
        output_model=ReportNodeRouteDecision,
        task=_PROMPT_TASK,
        temperature=0.0,
        metadata={"node": "report_node_routing"},
    )


# 方法作用：为一次报表单元执行决定节点序列。
# Args: question - 分析单元问题；allow_multi_source - 本次是否有多个授权数据源
#       （模型看不到这个事实，必须由调用方按实际情况给出）。
# Returns: 规范顺序下的节点序列；模型不可用或异常时返回空列表（调用方退回静态图）。
async def decide_node_sequence(question: str, *, allow_multi_source: bool = False) -> list[str]:
    # 兜底范围必须覆盖到取值与收敛：模型层可能返回任何类型（历史上出现过把别的
    # 结构化输出串到这里的情况），只有把整段纳入异常兜底才真正满足"任何异常都退回静态图"。
    try:
        catalog = render_node_catalog()
        decision = await _invoke_node_route(question, catalog)
        if decision is None:
            return []
        nodes = list(getattr(decision, "nodes", []) or [])
        logger.info(
            "节点路由决策完成",
            node_count=len(nodes),
            allow_multi_source=bool(allow_multi_source),
            reason=str(getattr(decision, "reason", "") or "")[:120],
        )
        return sanitize_node_sequence(nodes, allow_multi_source=allow_multi_source)
    except Exception:
        # 决策失败不影响执行：退回静态图并保留堆栈，与"模型没选到"区分开。
        logger.error("节点路由决策失败，退回静态图", exc_info=True)
        return []


# 方法作用：按节点序列现场编译保留安全条件边的受控工作流。
# Args: sequence - 已经过护栏收敛的节点序列。
# Returns: 已编译的 LangGraph 应用；序列非法或编译失败时返回 None。
async def build_sequence_graph(sequence: list[str]) -> Any | None:
    if not sequence:
        return None
    missing = [name for name in sequence if get_node_definition(name) is None]
    if missing:
        logger.error("节点路由序列含未注册节点，退回静态图", missing=missing)
        return None
    try:
        from langgraph.graph import END, StateGraph

        from src.graph.state import AnalysisState
        from src.memory.checkpointer import get_checkpointer

        selected = set(sequence)
        workflow: Any = StateGraph(AnalysisState)
        is_sql_flow = "execute_sql" in selected and _SQL_FLOW_NODES <= selected
        is_multi_source = _MULTI_SOURCE_FLOW <= selected
        is_direct = "llm_direct_answer" in selected
        if is_sql_flow:
            from src.graph.subgraphs import sql_analysis as sql_flow
            from src.graph.workflow import (
                after_generate_sql,
                after_layer3,
                after_layer4,
                after_retrieve_schema,
                should_retry,
            )

            non_sql_nodes = [name for name in sequence if name not in _SQL_FLOW_NODES]
            for name in non_sql_nodes:
                definition = get_node_definition(name)
                workflow.add_node(name, definition.handler)
            sql_flow.add_sql_analysis_flow(
                workflow,
                after_retrieve_schema=after_retrieve_schema,
                after_generate_sql=after_generate_sql,
                after_layer3=after_layer3,
                after_layer4=after_layer4,
                should_retry=should_retry,
                direct_target="build_response",
                failure_target="build_response",
                success_target=(
                    "analyze_result"
                    if "analyze_result" in selected
                    else "generate_chart"
                    if "generate_chart" in selected
                    else "build_response"
                ),
            )
            workflow.set_entry_point("prepare_turn")
            if "classify_intent" in selected:
                workflow.add_edge("prepare_turn", "classify_intent")
                workflow.add_edge("classify_intent", "retrieve_schema")
            else:
                workflow.add_edge("prepare_turn", "retrieve_schema")
            if "analyze_result" in selected:
                workflow.add_edge(
                    "analyze_result",
                    "generate_chart" if "generate_chart" in selected else "build_response",
                )
            if "generate_chart" in selected:
                workflow.add_edge("generate_chart", "build_response")
            workflow.add_edge("build_response", END)
        elif is_multi_source:
            branch = [
                name
                for name in ("prepare_turn", "classify_intent", "multi_source_dispatch", "merge_results", "build_response")
                if name in selected
            ]
            for name in branch:
                definition = get_node_definition(name)
                workflow.add_node(name, definition.handler)
            workflow.set_entry_point("prepare_turn")
            for previous, current in zip(branch, branch[1:], strict=False):
                workflow.add_edge(previous, current)
            workflow.add_edge("build_response", END)
        elif is_direct:
            branch = [
                name
                for name in ("prepare_turn", "classify_intent", "retrieve_schema", "llm_direct_answer", "build_response")
                if name in selected
            ]
            for name in branch:
                definition = get_node_definition(name)
                workflow.add_node(name, definition.handler)
            workflow.set_entry_point("prepare_turn")
            for previous, current in zip(branch, branch[1:], strict=False):
                workflow.add_edge(previous, current)
            workflow.add_edge("build_response", END)
        else:
            logger.warning("节点路由序列无法形成完整分支，退回静态图", sequence=sequence)
            return None
        compiled = workflow.compile(checkpointer=await get_checkpointer())
    except Exception:
        logger.error("节点路由建模失败，退回静态图", sequence=sequence, exc_info=True)
        return None
    logger.info("节点路由建模完成", sequence=sequence)
    return compiled
