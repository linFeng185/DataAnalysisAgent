"""11.3 SSE 流式输出 — astream_events 逐 Node 推送。

并行 LLM 调用支持：
  - thinking / token 事件均携带 node 字段标识来源节点
  - llm_content_parts 按节点分区，互不污染
  - on_chat_model_stream 独立查找父节点，不依赖全局状态
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from src.logging_config import get_logger

logger = get_logger(__name__)


def _json_serialize(obj):
    """JSON 序列化器，处理 date/datetime/Decimal 等非原生类型。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Type {type(obj)} not serializable")

# 需要推送 progress 事件的关键节点及其描述
_PROGRESS_MAP: dict[str, str] = {
    "classify_intent": "正在分析问题意图...",
    "retrieve_schema": "正在检索数据库表结构...",
    "generate_sql": "正在生成 SQL 查询...",
    "layer3_validate": "正在校验 SQL 安全性...",
    "layer4_explain": "正在模拟执行 SQL...",
    "execute_sql": "正在执行查询...",
    "analyze_result": "正在分析查询结果...",
    "generate_chart": "正在生成图表...",
    "build_response": "正在组装响应...",
}


async def stream_analysis(user_query: str, datasource: str, session_id: str = ""):
    """SSE: 逐 Node 推送进度 + LLM token + 关键结果。

    每个 thinking / token 事件均携带 node 字段，
    前端可按 node 分组渲染并行 LLM 调用的输出。
    """
    from src.graph.workflow import app

    import uuid
    effective_id = session_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": effective_id}}
    if session_id:
        logger.info("会话续用", session_id=session_id[:20])
    else:
        logger.info("新会话", session_id=effective_id[:20])

    stats = {"chain_start": 0, "chain_end": 0, "chat_model_stream": 0, "chat_model_start": 0,
             "thinking_events": 0, "token_events": 0}
    llm_content_parts: dict[str, list[str]] = defaultdict(list)
    active_llm_nodes: set[str] = set()

    try:
        async for event in app.astream_events(
            {"user_query": user_query, "datasource": datasource}, config, version="v2"
        ):
            kind = event["event"]

            # ---- 链开始 (Node 进入) ----
            if kind == "on_chain_start":
                name = event.get("name", "")
                if name and not name.startswith("Runnable") and name != "LangGraph":
                    stats["chain_start"] += 1
                    yield _sse("node_start", {"node": name})
                    msg = _PROGRESS_MAP.get(name)
                    if msg:
                        yield _sse("progress", {"node": name, "message": msg})

            # ---- 链结束 (Node 完成) ----
            elif kind == "on_chain_end":
                name = event.get("name", "")
                output = event.get("data", {}).get("output", {})
                if name and not name.startswith("Runnable") and name != "LangGraph":
                    stats["chain_end"] += 1
                    yield _sse("node_end", {"node": name})
                    if name == "generate_sql" and isinstance(output, dict) and "generated_sql" in output:
                        yield _sse("sql", {"sql": output["generated_sql"]})
                    elif name == "layer3_validate":
                        yield _sse("validation", {"valid": output.get("sql_valid", True)})
                    elif name == "build_response" and isinstance(output, dict):
                        yield _sse("result", output.get("final_response", {}))
                    elif name == "analyze_result" and isinstance(output, dict):
                        yield _sse("analysis", output.get("analysis_result", {}))
                    # 回退：该节点 LLM 流式 token 没到时，输出累积内容
                    if name in active_llm_nodes and llm_content_parts.get(name):
                        yield _sse("token", {"node": name, "content": "".join(llm_content_parts[name])})

            # ---- LLM 流式开始 ----
            elif kind == "on_chat_model_start":
                stats["chat_model_start"] += 1
                node = _find_parent_node(event)
                if node:
                    active_llm_nodes.add(node)
                    llm_content_parts[node].clear()
                yield _sse("llm_start", {"node": node or "unknown"})

            # ---- LLM 流式 token ----
            elif kind == "on_chat_model_stream":
                stats["chat_model_stream"] += 1
                chunk = event.get("data", {}).get("chunk")
                if chunk:
                    # ChatGenerationChunk 包裹了 AIMessageChunk，需解包
                    if hasattr(chunk, "message") and not hasattr(chunk, "additional_kwargs"):
                        if stats["chat_model_stream"] == 1:
                            logger.info("LLM 流式 chunk 类型: ChatGenerationChunk, 自动解包")
                        chunk = chunk.message
                    elif stats["chat_model_stream"] == 1:
                        logger.info("LLM 流式 chunk 类型", type=type(chunk).__name__)

                    # 从事件 metadata 独立查找父节点（并行 LLM 时各自归因）
                    node = _find_parent_node(event)

                    from src.llm.adapters.registry import get_adapter
                    from src.config import get_settings
                    adapter = get_adapter(get_settings().llm_model)
                    sc = adapter.parse_stream_chunk(chunk)
                    if sc.reasoning_content:
                        stats["thinking_events"] += 1
                        yield _sse("thinking", {"node": node, "reasoning_content": sc.reasoning_content})
                    if sc.content:
                        stats["token_events"] += 1
                        if node:
                            llm_content_parts[node].append(sc.content)
                        yield _sse("token", {"node": node, "content": sc.content})

            # ---- LLM 流式结束 ----
            elif kind == "on_chat_model_end":
                node = _find_parent_node(event)
                if node:
                    active_llm_nodes.discard(node)
                yield _sse("llm_end", {"node": node})

    except Exception as e:
        logger.error("流式错误", error=str(e))
        yield _sse("error", {"message": str(e)})

    logger.info("流式完成", stats=stats, had_stream_tokens=stats["chat_model_stream"] > 0,
                thinking=stats["thinking_events"], tokens=stats["token_events"])
    yield _sse("done", {"status": "complete"})


def _sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event, **data}, ensure_ascii=False, default=_json_serialize)}\n\n"


def _find_parent_node(event: dict) -> str | None:
    """从事件 metadata 中提取父节点名称。"""
    metadata = event.get("metadata", {})
    if isinstance(metadata, dict):
        for key in ("langgraph_node", "parent_name", "checkpoint_ns"):
            val = metadata.get(key, "")
            if val and isinstance(val, str) and not val.startswith("Runnable"):
                return val
    tags = event.get("tags", [])
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and not tag.startswith("Runnable") and tag != "LangGraph":
                return tag
    return None
