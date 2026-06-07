"""11.3 SSE 流式输出 — astream_events 逐 Node 推送。"""

from __future__ import annotations

import json
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


async def stream_analysis(user_query: str, datasource: str):
    """SSE: 逐 Node 推送进度 + LLM token + 关键结果。"""
    from src.graph.workflow import app

    stats = {"chain_start": 0, "chain_end": 0, "chat_model_stream": 0, "chat_model_start": 0,
             "thinking_events": 0, "token_events": 0}
    llm_content_parts: list[str] = []
    current_llm_node: str | None = None

    try:
        async for event in app.astream_events(
            {"user_query": user_query, "datasource": datasource}, version="v2"
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
                    # 回退：LLM 流式 token 没到时，从 chain_end 输出提取
                    if name == current_llm_node and stats["chat_model_stream"] == 0 and llm_content_parts:
                        yield _sse("token", {"content": "".join(llm_content_parts)})

            # ---- LLM 流式开始 ----
            elif kind == "on_chat_model_start":
                stats["chat_model_start"] += 1
                current_llm_node = _find_parent_node(event)
                llm_content_parts.clear()
                yield _sse("llm_start", {"node": current_llm_node or "unknown"})

            # ---- LLM 流式 token ----
            elif kind == "on_chat_model_stream":
                stats["chat_model_stream"] += 1
                chunk = event.get("data", {}).get("chunk")
                if chunk:
                    # ChatGenerationChunk 包裹了 AIMessageChunk，需解包
                    # (LangGraph 某些版本传递原始 ChatGenerationChunk 而非内部 message)
                    if hasattr(chunk, "message") and not hasattr(chunk, "additional_kwargs"):
                        if stats["chat_model_stream"] == 1:
                            logger.info("LLM 流式 chunk 类型: ChatGenerationChunk, 自动解包")
                        chunk = chunk.message
                    elif stats["chat_model_stream"] == 1:
                        logger.info("LLM 流式 chunk 类型", type=type(chunk).__name__)
                    from src.llm.adapters.registry import get_adapter
                    from src.config import get_settings
                    adapter = get_adapter(get_settings().llm_model)
                    sc = adapter.parse_stream_chunk(chunk)
                    if sc.reasoning_content:
                        stats["thinking_events"] += 1
                        yield _sse("thinking", {"reasoning_content": sc.reasoning_content})
                    if sc.content:
                        stats["token_events"] += 1
                        llm_content_parts.append(sc.content)
                        yield _sse("token", {"content": sc.content})

            # ---- LLM 流式结束 ----
            elif kind == "on_chat_model_end":
                current_llm_node = None
                yield _sse("llm_end", {})

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
