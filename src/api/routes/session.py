"""查询历史与会话恢复路由。"""

from __future__ import annotations

import io
import html
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from src.api.schemas import (
    ChatRequest, ChatResponse, ColumnCommentRequest,
    DataSourceCreateRequest, DataSourceInfo, HealthResponse, KnowledgeTagCreateRequest,
    KnowledgeTagStatusRequest, MCPServerCreate, TableInfo,
)
from src.exceptions import DataSourceNotFoundError
from src.llm.client import is_llm_available
from src.logging_config import get_logger
from src.api.routes._helpers import _app, _authorize_extension_scope, _registry

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()

# ---- 查询历史 ----


@router.get("/history")
async def list_history(
    datasource: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    """分页列出查询历史，PG 持久化、重启不丢失。"""
    from src.memory.history_store import get_history_store
    return await get_history_store().list(
        datasource=datasource, search=search, page=page, page_size=page_size)


# ---- 会话管理 ----


@router.get("/sessions")
async def list_sessions(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """游标分页列出历史会话，按最近活跃时间倒序。

    cursor 为上一页最后一条的 last_active_at ISO 字符串，首次传空。
    """
    from src.memory.session_store import get_session_store

    logger.debug("列出会话路由入口", cursor=cursor, limit=limit)
    items = await get_session_store().list(cursor=cursor, limit=limit + 1)
    has_more = len(items) > limit
    page_items = items[:limit]
    next_cursor = page_items[-1]["last_active_at"] if has_more and page_items else None
    logger.info(
        "列出会话路由完成", count=len(page_items), has_more=has_more,
        next_cursor=next_cursor,
    )
    return {"sessions": page_items, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """获取会话详情，包含最近 20 轮对话 + 最新一轮的富数据。

    从 PG 读取会话元数据 + 从 LangGraph Checkpointer 读取对话内容。
    额外返回 latest_state 用于还原最后一轮的完整 UI（图表、数据表、分析结论等）。
    """
    from src.memory.session_store import get_session_store
    session = await get_session_store().get(session_id)
    if not session:
        raise HTTPException(404, f"会话 '{session_id}' 未找到")

    import src.api.routes as routes_package

    loaded_turns = await routes_package._load_session_turns(session_id, limit=21)
    has_more = len(loaded_turns) > 20
    turns = loaded_turns[-20:]
    # 提取最新一轮的富数据用于前端还原完整 UI
    latest_state = await routes_package._load_latest_state(session_id)
    if turns:
        # 持久化逐轮结果是权威数据，checkpoint 只补充缺失字段，避免贫化状态覆盖完整响应。
        latest_state = _merge_rich_result(
            turns[-1].get("final_result", {}) or {}, latest_state or {},
        )
    if turns and latest_state:
        # latest_state 只对应会话最后一轮，禁止向更早轮次扩散。
        turns[-1] = {
            **turns[-1],
            "sql": latest_state.get("sql", "") or turns[-1].get("sql", ""),
            "assistant_summary": (
                (latest_state.get("analysis", {}) or {}).get("summary", "")
                or turns[-1].get("assistant_summary", "")
            ),
            "final_result": latest_state,
        }
        logger.info(
            "会话最后一轮富数据合并完成",
            session_id=session_id[:20],
            turn_id=turns[-1].get("turn_id", 0),
            sql_statements=len(latest_state.get("sql_statements", []) or []),
            data_rows=len(latest_state.get("data", []) or []),
        )
    logger.info(
        "会话详情输出探针", session_id=session_id[:20],
        turns=len(turns), has_more=has_more,
        rich_turns=sum(1 for turn in turns if turn.get("final_result")),
        sql_turns=sum(1 for turn in turns if turn.get("sql")),
        data_rows=sum(
            len((turn.get("final_result", {}) or {}).get("data", []) or [])
            for turn in turns
        ),
    )
    return {
        "session": session, "turns": turns,
        "latest_state": latest_state, "has_more": has_more,
    }


@router.get("/sessions/{session_id}/turns")
async def list_session_turns(
    session_id: str,
    before: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """瀑布流加载会话的对话轮次。

    before 为轮次序号，只返回该序号之前的更早轮次。
    不传 before 返回最新的 limit 条。
    """
    from src.memory.session_store import get_session_store
    if await get_session_store().get(session_id) is None:
        logger.warning("拒绝读取无权会话轮次", session_id=session_id[:20])
        raise HTTPException(404, f"会话 '{session_id}' 未找到")
    import src.api.routes as routes_package

    loaded_turns = await routes_package._load_session_turns(
        session_id,
        before=before,
        limit=limit + 1,
    )
    has_more = len(loaded_turns) > limit
    turns = loaded_turns[-limit:]
    return {"turns": turns, "has_more": has_more}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除当前身份拥有的会话元数据、历史和 Checkpointer state。"""
    from src.api.auth import scope_thread_id
    from src.memory.checkpointer import get_checkpointer
    from src.memory.history_store import get_history_store
    from src.memory.session_store import get_session_store

    logger.debug("删除会话路由入口", session_id=session_id[:20])
    session_store = get_session_store()
    if await session_store.get(session_id) is None:
        logger.warning("删除会话目标不可见", session_id=session_id[:20])
        raise HTTPException(404, "会话不存在")
    await get_history_store().delete_session(session_id)
    checkpointer = await get_checkpointer()
    scoped_thread_id = scope_thread_id(session_id)
    await checkpointer.adelete_thread(scoped_thread_id)
    await checkpointer.adelete_thread(session_id)
    ok = await session_store.delete(session_id)
    if not ok:
        logger.error("删除会话元数据失败", session_id=session_id[:20])
        raise HTTPException(500, "删除失败")
    logger.info("删除会话路由完成", session_id=session_id[:20])
    return {"status": "ok", "session_id": session_id}


# 方法作用：按字段优先级合并持久化富结果与 checkpoint 回退结果。
# Args: primary - 权威的逐轮持久化结果；fallback - checkpoint 或兼容路径结果。
# Returns: 合并后的结构化响应字典。
def _merge_rich_result(primary: dict | None, fallback: dict | None) -> dict:
    """以持久化响应为主，仅用回退响应补齐空的富数据字段。"""
    try:
        primary_value = primary if isinstance(primary, dict) else {}
        fallback_value = fallback if isinstance(fallback, dict) else {}
        logger.debug(
            "合并历史富结果入口",
            primary_keys=sorted(primary_value.keys()),
            fallback_keys=sorted(fallback_value.keys()),
        )
        merged = dict(fallback_value)
        merged.update(primary_value)
        rich_fields = (
            "sql", "sql_statements", "data", "row_count", "analysis", "chart",
            "sql_reasoning_content",
        )
        for field in rich_fields:
            value = primary_value.get(field)
            if value is None or value == "" or value == [] or value == {} or value == 0:
                if field in fallback_value and fallback_value[field] not in (None, "", [], {}, 0):
                    merged[field] = fallback_value[field]
        logger.info(
            "合并历史富结果完成",
            primary_rich=bool(primary_value),
            fallback_rich=bool(fallback_value),
            sql_statements=len(merged.get("sql_statements", []) or []),
            data_rows=len(merged.get("data", []) or []),
        )
        return merged
    except Exception as exc:
        logger.error("合并历史富结果失败", error=str(exc), exc_info=True)
        return dict(primary or fallback or {})


async def _load_checkpoint_tuple(session_id: str) -> object | None:
    """优先读取当前身份命名空间，并兼容迁移前的原始会话线程。

    Args:
        session_id: 对外会话 ID。

    Returns:
        最新 checkpoint tuple；两个线程均无状态时返回 None。
    """
    from src.api.auth import scope_thread_id
    from src.memory.checkpointer import get_checkpointer

    cp = await get_checkpointer()
    scoped_thread_id = scope_thread_id(session_id)
    logger.debug(
        "加载会话 checkpoint 入口", session_id=session_id[:20],
        scoped_thread_id=scoped_thread_id[-60:], checkpointer=type(cp).__name__,
    )
    try:
        scoped_config = {"configurable": {"thread_id": scoped_thread_id}}
        checkpoint_tuple = await cp.aget_tuple(scoped_config)
        if checkpoint_tuple:
            logger.info(
                "加载会话 checkpoint 完成", session_id=session_id[:20],
                source="scoped",
            )
            return checkpoint_tuple

        logger.info(
            "命名空间 checkpoint 无状态，回退旧会话线程",
            session_id=session_id[:20], legacy_thread_id=session_id[:20],
        )
        legacy_config = {"configurable": {"thread_id": session_id}}
        checkpoint_tuple = await cp.aget_tuple(legacy_config)
        logger.info(
            "加载会话 checkpoint 完成", session_id=session_id[:20],
            source="legacy" if checkpoint_tuple else "missing",
        )
        return checkpoint_tuple
    except Exception as exc:
        logger.error(
            "加载会话 checkpoint 失败", session_id=session_id[:20],
            error=str(exc), exc_info=True,
        )
        raise


async def _load_latest_state(session_id: str) -> dict | None:
    """从 Checkpointer 加载最新一轮的富数据（分析结论、图表、数据样本）。

    用于前端恢复历史会话时还原完整的分析结果 UI。
    """
    try:
        import src.api.routes as routes_package

        tup = await routes_package._load_checkpoint_tuple(session_id)
        if not tup:
            logger.info("最新状态 Checkpointer 无状态", session_id=session_id[:20])
            tup = None
        if tup:
            cv = tup.checkpoint.get("channel_values", {}) or {}
        else:
            cv = {}
        checkpoint_response = cv.get("final_response", {}) or {}
        logger.info(
            "历史最新状态字段探针",
            session_id=session_id[:20],
            channel_keys=sorted(str(key) for key in cv.keys()),
            has_final_response=bool(checkpoint_response),
            generated_sql=bool(cv.get("generated_sql")),
            final_sql=bool(checkpoint_response.get("sql"))
            if isinstance(checkpoint_response, dict) else False,
            final_sql_statements=len(checkpoint_response.get("sql_statements", []) or [])
            if isinstance(checkpoint_response, dict) else 0,
            final_data_rows=len(checkpoint_response.get("data", []) or [])
            if isinstance(checkpoint_response, dict) else 0,
        )
        # 多源查询的 generated_sql 可以为空，必须优先读取最终响应判断富数据。
        if isinstance(checkpoint_response, dict) and checkpoint_response:
            data_sample = checkpoint_response.get("data", cv.get("query_result_sample", [])) or []
            result = {
                "sql": checkpoint_response.get("sql", cv.get("generated_sql", "")) or "",
                "sql_statements": checkpoint_response.get("sql_statements", []) or [],
                "analysis": checkpoint_response.get("analysis", cv.get("analysis_result", {})) or {},
                "chart": checkpoint_response.get("chart", cv.get("chart_config", {})) or {},
                "data": data_sample if isinstance(data_sample, list) else [],
                "row_count": int(
                    checkpoint_response.get("row_count", cv.get("query_result_full_count", 0)) or 0
                ),
                "truncated": bool(
                    checkpoint_response.get("truncated", cv.get("query_result_truncated", False))
                ),
                "success": bool(checkpoint_response.get("success", True)),
                "error_message": checkpoint_response.get("error_message", "") or "",
                "sql_reasoning_content": checkpoint_response.get(
                    "sql_reasoning_content", cv.get("sql_reasoning_content", "")
                ) or "",
            }
            logger.info(
                "最新状态从最终响应恢复", session_id=session_id[:20],
                sql_statements=len(result["sql_statements"]),
                data_rows=len(result["data"]),
                has_analysis=bool(result["analysis"]),
            )
            return result

        # Checkpointer 不可用或没有最终响应时，回退到持久化逐轮查询历史。
        if not cv.get("generated_sql") and not cv.get("execution_error"):
            from src.memory.history_store import get_history_store
            history = await get_history_store().list_session(session_id, limit=1)
            if history:
                latest = history[-1]
                persisted_response = latest.get("final_result", {}) or {}
                if isinstance(persisted_response, dict) and persisted_response:
                    logger.info(
                        "最新状态从持久化结构化响应恢复",
                        session_id=session_id[:20],
                        data_rows=len(persisted_response.get("data", []) or []),
                    )
                    return persisted_response
                logger.info(
                    "最新状态从查询历史恢复", session_id=session_id[:20],
                    sql=bool(latest.get("sql")),
                )
                return {
                    "sql": latest.get("sql", "") or "",
                    "sql_statements": [],
                    "analysis": {}, "chart": {}, "data": [],
                    "row_count": int(latest.get("row_count", 0) or 0),
                    "truncated": False,
                    "success": bool(latest.get("success", True)),
                    "error_message": "" if latest.get("success", True) else "查询失败",
                    "sql_reasoning_content": "",
                }
        # 只提取前端需要的富数据字段
        data_sample = cv.get("query_result_sample", []) or []
        return {
            "sql": cv.get("generated_sql", "") or "",
            "sql_statements": [],
            "analysis": cv.get("analysis_result", {}) or {},
            "chart": cv.get("chart_config", {}) or {},
            "data": data_sample if isinstance(data_sample, list) else [],
            "row_count": int(cv.get("query_result_full_count", 0) or 0),
            "truncated": bool(cv.get("query_result_truncated", False)),
            "success": not cv.get("execution_error", ""),
            "error_message": cv.get("execution_error", "") or "",
            "sql_reasoning_content": cv.get("sql_reasoning_content", "") or "",
        }
    except Exception as exc:
        logger.error(
            "最新状态加载失败",
            session_id=session_id[:20],
            error=str(exc),
            exc_info=True,
        )
        raise


# 方法作用：把 checkpoint 中的结构化历史、消息或当前输入转换为统一轮次。
# Args: channel_values - LangGraph checkpoint 的 channel_values。
# Returns: 按轮次正序排列的会话轮次列表。
def _checkpoint_turns(channel_values: dict) -> list[dict]:
    logger.debug("解析 checkpoint 轮次入口", channel_count=len(channel_values))
    turns: list[dict] = []
    checkpoint_history = channel_values.get("conversation_history", []) or []
    messages = channel_values.get("messages", []) or []
    if checkpoint_history:
        for index, item in enumerate(checkpoint_history):
            value = item if isinstance(item, dict) else {
                "turn_id": getattr(item, "turn_id", index + 1),
                "user_query": getattr(item, "user_query", ""),
                "generated_sql": getattr(item, "generated_sql", ""),
                "analysis_summary": getattr(item, "analysis_summary", ""),
                "timestamp": getattr(item, "timestamp", ""),
                "final_result": getattr(item, "final_result", {}),
            }
            if not value.get("user_query"):
                continue
            final_result = value.get("final_result", {}) or {}
            analysis = final_result.get("analysis", {}) if isinstance(final_result, dict) else {}
            turns.append({
                "turn_id": value.get("turn_id", index + 1),
                "user_query": value.get("user_query", "") or "",
                "assistant_summary": (
                    value.get("analysis_summary", "")
                    or (analysis.get("summary", "") if isinstance(analysis, dict) else "")
                    or ""
                ),
                "sql": (
                    final_result.get("sql", "") if isinstance(final_result, dict) else ""
                ) or value.get("generated_sql", "") or "",
                "timestamp": str(value.get("timestamp", "") or ""),
                "final_result": final_result if isinstance(final_result, dict) else {},
            })
    elif messages:
        index = 0
        while index < len(messages):
            message = messages[index]
            if type(message).__name__ == "HumanMessage":
                user_query = (
                    message.content if isinstance(message.content, str) else str(message.content)
                )
                sql = ""
                summary = ""
                next_index = index + 1
                while next_index < len(messages) and next_index < index + 4:
                    candidate = messages[next_index]
                    if type(candidate).__name__ == "AIMessage":
                        content = (
                            candidate.content
                            if isinstance(candidate.content, str)
                            else str(candidate.content)
                        )
                        if content.startswith("SQL: "):
                            parts = content.split("\n结论: ", 1)
                            sql = parts[0][5:] if parts else ""
                            summary = parts[1] if len(parts) > 1 else content[5:]
                        else:
                            summary = content
                        index = next_index
                        break
                    next_index += 1
                turns.append({
                    "turn_id": len(turns) + 1,
                    "user_query": user_query,
                    "assistant_summary": summary,
                    "sql": sql,
                    "timestamp": "",
                    "final_result": {},
                })
            index += 1
    elif channel_values.get("user_query"):
        analysis = channel_values.get("analysis_result", {}) or {}
        summary = analysis.get("summary", "") if isinstance(analysis, dict) else ""
        turns.append({
            "turn_id": 1,
            "user_query": channel_values.get("user_query", "") or "",
            "assistant_summary": summary or channel_values.get("execution_error", "") or "会话未完成",
            "sql": channel_values.get("generated_sql", "") or "",
            "timestamp": "",
            "final_result": {},
        })
    logger.info("解析 checkpoint 轮次完成", turns=len(turns))
    return turns


# 方法作用：用 query_history 富数据补齐 checkpoint 轮次和旧记录最小响应。
# Args: session_id - 会话 ID；before - 轮次游标；limit - 条数上限；turns - checkpoint 轮次。
# Returns: 合并持久化历史并补齐 final_result 的轮次列表。
async def _enrich_turns_from_history(
    session_id: str,
    before: int | None,
    limit: int,
    turns: list[dict],
) -> list[dict]:
    logger.debug(
        "历史富数据补齐入口",
        session_id=session_id[:20],
        before=before,
        limit=limit,
        checkpoint_turns=len(turns),
    )
    from src.memory.history_store import get_history_store

    history = await get_history_store().list_session(
        session_id,
        before=None if turns else before,
        limit=1000 if turns else limit,
    )
    persisted_turns = [{
        "turn_id": item.get("turn_id", index + 1),
        "user_query": item.get("query", "") or "",
        "assistant_summary": (
            ((item.get("final_result", {}) or {}).get("analysis", {}) or {}).get("summary", "")
            or (
                f"查询成功，返回 {item.get('row_count', 0) or 0} 行"
                if item.get("success", True)
                else "查询失败"
            )
        ),
        "sql": item.get("sql", "") or "",
        "timestamp": item.get("time", "") or "",
        "final_result": item.get("final_result", {}) or {},
    } for index, item in enumerate(history)]
    if not turns:
        turns = persisted_turns
        logger.info("会话轮次从查询历史恢复", session_id=session_id[:20], turns=len(turns))
    elif persisted_turns:
        persisted_by_id = {item["turn_id"]: item for item in persisted_turns}
        for turn in turns:
            persisted = persisted_by_id.get(turn["turn_id"])
            if not persisted:
                continue
            if persisted.get("final_result"):
                turn["final_result"] = _merge_rich_result(
                    persisted["final_result"],
                    turn.get("final_result", {}) or {},
                )
            turn["sql"] = persisted.get("sql") or turn.get("sql", "")
            turn["timestamp"] = persisted.get("timestamp") or turn.get("timestamp", "")
            if not turn.get("assistant_summary"):
                turn["assistant_summary"] = persisted.get("assistant_summary", "")
        logger.info(
            "会话轮次结构化响应合并完成",
            session_id=session_id[:20],
            persisted_turns=len(persisted_turns),
            rich_turns=sum(1 for turn in turns if turn.get("final_result")),
        )

    for turn in turns:
        if turn.get("final_result"):
            continue
        turn["final_result"] = {
            "success": True,
            "sql": turn.get("sql", ""),
            "sql_statements": [],
            "data": [],
            "row_count": 0,
            "truncated": False,
            "analysis": {
                "summary": turn.get("assistant_summary", ""),
                "insights": [],
                "recommended_chart_type": "table",
            },
            "chart": {"type": "table", "option": {}},
        }
    logger.info("历史富数据补齐完成", session_id=session_id[:20], turns=len(turns))
    return turns


async def _load_session_turns(session_id: str, before: int | None = None, limit: int = 20) -> list[dict]:
    """从 LangGraph Checkpointer 加载会话的对话轮次。

    Args:
        session_id - 会话 ID（即 thread_id）
        before - 轮次序号游标，只返回此序号之前的轮次
        limit - 返回条数上限

    Returns: 对话轮次列表
    """
    try:
        logger.debug(
            "加载会话轮次边界", session_id=session_id[:20],
            before=before, limit=limit,
        )
        # 使用最新 checkpoint 恢复完整摘要，并用持久化历史补齐逐轮富数据。
        import src.api.routes as routes_package

        tup = await routes_package._load_checkpoint_tuple(session_id)
        channel_values: dict = {}
        if not tup:
            logger.info("Checkpointer 无状态", session_id=session_id[:20])
            messages = []
        else:
            channel_values = tup.checkpoint.get("channel_values", {}) or {}
            messages = channel_values.get("messages", []) or []
        checkpoint_history_probe = channel_values.get("conversation_history", []) or []
        logger.info(
            "历史轮次结构探针",
            session_id=session_id[:20],
            message_count=len(messages),
            history_count=len(checkpoint_history_probe),
            rich_history_count=sum(
                1 for item in checkpoint_history_probe
                if isinstance(item, dict) and bool(item.get("final_result"))
            ),
            checkpoint_final_response=bool(channel_values.get("final_response")),
        )
        logger.info("会话轮次加载", session_id=session_id[:20], msg_count=len(messages))

        turns = _checkpoint_turns(channel_values)
        if turns:
            logger.info(
                "会话轮次从 checkpoint 恢复",
                session_id=session_id[:20],
                turns=len(turns),
            )

        turns = await _enrich_turns_from_history(session_id, before, limit, turns)

        candidates = [turn for turn in turns if before is None or turn["turn_id"] < before]
        result = candidates[-limit:]

        logger.info(
            "会话轮次解析完成", session_id=session_id[:20],
            turns=len(result), total_candidates=len(candidates),
            rich_turns=sum(1 for turn in result if turn.get("final_result")),
        )
        return result
    except Exception as exc:
        logger.error(
            "会话轮次加载失败",
            session_id=session_id[:20],
            error=str(exc),
            exc_info=True,
        )
        raise
