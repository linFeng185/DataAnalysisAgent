"""11.3 SSE 流式输出 — astream_events 逐 Node 推送。

并行 LLM 调用支持：
  - thinking / token 事件均携带 node 与 stream_id 标识调用实例
  - llm_content_parts 按 stream_id 分区，互不污染
  - on_chat_model_stream 独立查找父节点，不依赖全局状态
"""

from __future__ import annotations

import json
from collections import defaultdict
from src.config import get_settings
from datetime import date, datetime
from decimal import Decimal

from src.logging_config import get_logger

logger = get_logger(__name__)


def _clean_float(f: float) -> Decimal:
    """float → Decimal，智能检测 IEEE 754 噪声并去除。

    规律：IEEE 754 噪声在十进制中表现为小数末尾出现连续4个以上的 0 或 9。
    例如 532917884.0400004 → "0400004" 结尾归到 .04。
    """
    s = str(f)
    if "." not in s or "e" in s.lower():
        return Decimal(s)
    integer_part, frac = s.split(".", 1)
    # 检测连续重复的 0 或 9（IEEE 噪声特征），从噪声起点截断
    noise_start = _find_noise(frac)
    if noise_start > 0:
        # 量化到噪声前精度，四舍五入
        return Decimal(s).quantize(Decimal(f"0.{'0'*noise_start}"))
    return Decimal(s)


def _find_noise(frac: str) -> int:
    """找到小数部分末尾噪声的起始位置。返回 0 表示无噪声。"""
    if len(frac) < 6:
        return 0
    # 从末位往前找，连续 >=4 个相同字符(0或9) = 噪声
    i = len(frac) - 1
    run_char = frac[i]
    run_len = 1
    while i > 0:
        i -= 1
        if frac[i] == run_char:
            run_len += 1
        else:
            if run_len >= 3 and run_char in "09":
                return i + 1  # 噪声起点
            run_char = frac[i]
            run_len = 1
    if run_len >= 3 and run_char in "09":
        return 0
    return 0


class _PrecisionEncoder(json.JSONEncoder):
    """遍历数据树，float → 智能清洗 → Decimal → 精确序列化。"""
    def encode(self, o):
        return super().encode(self._walk(o))

    @staticmethod
    def _walk(obj):
        if isinstance(obj, float) and not isinstance(obj, bool):
            return _clean_float(obj)
        if isinstance(obj, dict):
            return {k: _PrecisionEncoder._walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_PrecisionEncoder._walk(v) for v in obj]
        return obj

def _json_serialize(obj):
    """JSON 序列化器，Decimal 保持精确数值。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        normalized = obj.normalize()
        _, _, exp = normalized.as_tuple()
        if exp >= 0:
            return int(normalized)
        return float(normalized) if abs(exp) <= 12 else str(normalized)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Type {type(obj)} not serializable")

from src.graph.node_registry import get_progress_map


# 需要推送 progress 事件的节点文案由工作流节点目录统一生成。
_PROGRESS_MAP: dict[str, str] = get_progress_map()


# 提取 LangChain 事件的稳定模型调用标识。
# Args: event - astream_events 返回的单个事件。
# Returns: 优先使用 run_id；旧事件缺少 run_id 时返回节点兼容标识。
def _event_stream_id(event: dict) -> str:
    """同名并行节点必须按模型调用 run_id 隔离流式缓冲区。"""
    logger.debug("流式调用标识提取入口", event_name=event.get("name", ""))
    run_id = event.get("run_id")
    if run_id:
        stream_id = str(run_id)
    else:
        node = _find_parent_node(event) or str(event.get("name", "") or "unknown")
        stream_id = f"legacy:{node}"
    logger.debug("流式调用标识提取完成", stream_id=stream_id)
    return stream_id


# 方法作用：执行 LangGraph 并通过 SSE 推送节点进度、模型事件和最终结果。
# Args: user_query - 用户问题；datasource - 主数据源；session_id - 会话；datasources - 多数据源；tenant_id/user_id/user_role - 认证身份；datasource_access - API 授权快照；request_rate_limit_checked - API 是否已计入配额。
# Returns: SSE 事件异步生成器。
async def stream_analysis(user_query: str, datasource: str, session_id: str = "",
                          datasources: list[str] | None = None,
                          tenant_id: int | None = None, user_id: int | None = None,
                          user_role: str | None = None,
                          datasource_access: dict[str, dict] | None = None,
                          request_rate_limit_checked: bool = False):
    """SSE: 逐 Node 推送进度 + LLM token + 关键结果。

    每个 thinking / token 事件均携带 node 和 stream_id 字段，
    前端可按调用实例分组渲染并行 LLM 输出。
    """
    from src.graph.workflow import app

    import uuid
    effective_id = session_id or str(uuid.uuid4())
    from src.api.auth import (
        get_current_role, get_current_tenant_id, get_current_user_id, scope_thread_id,
    )
    tenant_id = get_current_tenant_id() if tenant_id is None else tenant_id
    user_id = get_current_user_id() if user_id is None else user_id
    user_role = get_current_role() if user_role is None else user_role
    selected_datasources = list(dict.fromkeys(
        str(name).strip() for name in (datasources or ([datasource] if datasource else []))
        if str(name).strip()
    ))
    if datasource_access is None:
        if get_settings().multi_tenant:
            logger.error("流式权限快照缺失", tenant_id=tenant_id, user_id=user_id)
            raise PermissionError("数据源权限快照缺失")
        datasource_access = {
            name: {"name": name, "allowed_columns": [], "row_filter_sql": ""}
            for name in selected_datasources
        }
    primary_access = datasource_access.get(datasource, {}) if datasource else {}
    logger.info(
        "流式权限快照就绪",
        authorized_count=len(datasource_access),
        selected_count=len(selected_datasources),
        discovery=not selected_datasources,
    )
    config = {"configurable": {"thread_id": scope_thread_id(effective_id)}}
    is_new = not session_id
    if is_new:
        logger.info("新会话", session_id=effective_id[:20])
        # 异步写入会话元数据（不影响主流程）
        import asyncio as _asyncio
        try:
            _asyncio.create_task(_save_session_meta(effective_id, datasource, user_query))
        except Exception as exc:
            logger.error(
                "新会话元数据任务创建失败",
                session_id=effective_id[:20],
                error=str(exc),
                exc_info=True,
            )
    else:
        logger.info("会话续用", session_id=session_id[:20])
        import asyncio as _asyncio
        try:
            _asyncio.create_task(_touch_session_meta(effective_id, datasource, user_query))
        except Exception as exc:
            logger.error(
                "会话活跃时间任务创建失败",
                session_id=effective_id[:20],
                error=str(exc),
                exc_info=True,
            )

    stats = {"chain_start": 0, "chain_end": 0, "chat_model_stream": 0, "chat_model_start": 0,
             "thinking_events": 0, "token_events": 0}
    llm_content_parts: dict[str, list[str]] = defaultdict(list)
    active_llm_streams: set[str] = set()
    stream_nodes: dict[str, str] = {}

    try:
        input_state = {"user_query": user_query, "datasource": datasource,
                       "session_id": effective_id,
                       "selected_datasources": selected_datasources,
                       "datasource_access": datasource_access,
                       "allowed_columns": list(primary_access.get("allowed_columns", []) or []),
                       "row_filter_sql": str(primary_access.get("row_filter_sql", "") or ""),
                       "tenant_id": tenant_id, "user_id": user_id,
                       "user_role": user_role,
                       "request_rate_limit_checked": request_rate_limit_checked}
        async for event in app.astream_events(input_state, config, version="v2"
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
                    # 重试透明：generate_sql 重试时发通知
                    if name == "generate_sql" and isinstance(output, dict):
                        rc = output.get("retry_count", 0)
                        if rc and rc > 1:
                            yield _sse("retry_status", {"retry": rc,
                                      "max": get_settings().max_retry_count,
                                      "reason": "SQL 生成/校验/执行失败，正在自动重试"})
                    if name == "generate_sql" and isinstance(output, dict) and "generated_sql" in output:
                        yield _sse("sql", {"sql": output["generated_sql"]})
                    elif name == "execute_sql" and isinstance(output, dict) and output.get("generated_sql"):
                        # 方言重写后的最终 SQL，覆盖 generate_sql 发送的原始版本
                        yield _sse("sql", {"sql": output["generated_sql"]})
                    elif name == "layer3_validate":
                        yield _sse("validation", {"valid": output.get("sql_valid", True)})
                    elif name == "build_response" and isinstance(output, dict):
                        yield _sse("result", output.get("final_response", {}))
                    elif name == "analyze_result" and isinstance(output, dict):
                        yield _sse("analysis", output.get("analysis_result", {}))
                    # 回退：该节点 LLM 流式 token 没到时，输出累积内容
                    pending_streams = [
                        stream_id for stream_id in active_llm_streams
                        if stream_nodes.get(stream_id) == name and llm_content_parts.get(stream_id)
                    ]
                    for stream_id in pending_streams:
                        yield _sse("token", {
                            "node": name,
                            "stream_id": stream_id,
                            "content": "".join(llm_content_parts[stream_id]),
                        })

            # ---- LLM 流式开始 ----
            elif kind == "on_chat_model_start":
                stats["chat_model_start"] += 1
                node = _find_parent_node(event)
                stream_id = _event_stream_id(event)
                active_llm_streams.add(stream_id)
                stream_nodes[stream_id] = node or "unknown"
                llm_content_parts[stream_id].clear()
                yield _sse("llm_start", {
                    "node": node or "unknown",
                    "stream_id": stream_id,
                })

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
                    stream_id = _event_stream_id(event)
                    stream_nodes.setdefault(stream_id, node or "unknown")

                    from src.llm.adapters.registry import get_adapter
                    adapter = get_adapter(get_settings().llm_model)
                    sc = adapter.parse_stream_chunk(chunk)
                    if sc.reasoning_content:
                        stats["thinking_events"] += 1
                        yield _sse("thinking", {
                            "node": node,
                            "stream_id": stream_id,
                            "reasoning_content": sc.reasoning_content,
                        })
                    if sc.content:
                        stats["token_events"] += 1
                        llm_content_parts[stream_id].append(sc.content)
                        yield _sse("token", {
                            "node": node,
                            "stream_id": stream_id,
                            "content": sc.content,
                        })

            # ---- LLM 流式结束 ----
            elif kind == "on_chat_model_end":
                node = _find_parent_node(event)
                stream_id = _event_stream_id(event)
                active_llm_streams.discard(stream_id)
                yield _sse("llm_end", {"node": node, "stream_id": stream_id})

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


async def _save_session_meta(session_id: str, datasource: str, first_query: str) -> None:
    """保存新会话元数据到 PG（fire-and-forget）。"""
    try:
        from src.memory.session_store import get_session_store
        await get_session_store().create(session_id, datasource, first_query)
    except Exception as e:
        logger.warning("会话元数据保存失败（非致命）", error=str(e), exc_info=True)


async def _touch_session_meta(session_id: str, datasource: str = "", first_query: str = "") -> None:
    """更新会话活跃时间（UPSERT，不存在自动创建）。"""
    try:
        from src.memory.session_store import get_session_store
        await get_session_store().touch(session_id, datasource, first_query)
    except Exception as e:
        logger.warning("会话元数据更新失败（非致命）", error=str(e), exc_info=True)
