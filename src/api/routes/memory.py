"""长期记忆管理路由：仅暴露当前登录用户 private 范围的读取与删除。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["memory"])


# 方法作用：把数据库行序列化为接口响应，解析 payload 并推导过期时间。
# Args: row - long_term_memories 行（含 payload/created_at/last_accessed_at）。
# Returns: 可直接返回给前端的字典。
def _serialize_memory(row: Any) -> dict[str, Any]:
    """payload 兼容字符串与对象，损坏数据降级为空对象。"""
    data = dict(row)
    logger.debug("长期记忆序列化入口", memory_id=str(data.get("id", "")))
    payload = data.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            logger.warning("长期记忆 payload 解析失败", memory_id=data.get("id"))
            payload = {}
    elif not isinstance(payload, dict):
        payload = {}
    created_at = data.get("created_at")
    last_accessed_at = data.get("last_accessed_at")
    ttl_days = data.get("ttl_days")
    expires_at = None
    if isinstance(created_at, datetime) and isinstance(ttl_days, int) and ttl_days > 0:
        expires_at = (created_at + timedelta(days=ttl_days)).astimezone(timezone.utc).isoformat()
    result = {
        "id": str(data.get("id", "")),
        "memory_type": str(data.get("memory_type", "")),
        "scope": str(data.get("scope", "")),
        "content": data.get("content") or "",
        "payload": payload,
        "access_count": int(data.get("access_count") or 0),
        "confidence": float(data.get("confidence") or 0.0),
        "ttl_days": ttl_days,
        "created_at": (
            created_at.astimezone(timezone.utc).isoformat()
            if isinstance(created_at, datetime) else None
        ),
        "last_accessed_at": (
            last_accessed_at.astimezone(timezone.utc).isoformat()
            if isinstance(last_accessed_at, datetime) else None
        ),
        "expires_at": expires_at,
    }
    logger.debug("长期记忆序列化完成", memory_id=result["id"])
    return result


# 方法作用：分页列出当前登录用户 private 范围的长期记忆。
# Args: memory_type - 类型过滤；search - 内容关键词；page/page_size - 分页。
# Returns: 记忆分页结果。
@router.get("/memory")
async def list_memories(
    memory_type: str = Query(default=""),
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
):
    """归属恒为当前登录用户，不接受请求参数覆盖。"""
    logger.debug(
        "长期记忆列表 API 入口",
        memory_type=memory_type,
        has_search=bool(search),
        page=page,
        page_size=page_size,
    )
    from src.memory.long_term_store import get_long_term_memory_store

    store = await get_long_term_memory_store()
    items, total = await store.list_memories(
        memory_type=memory_type, search=search, page=page, page_size=page_size,
    )
    result = {
        "items": [_serialize_memory(row) for row in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    logger.info("长期记忆列表 API 完成", total=total, returned=len(result["items"]))
    return result


# 方法作用：读取当前登录用户的单条长期记忆详情。
# Args: memory_id - 记忆 ID。
# Returns: 记忆详情；不存在或非本人时 404。
@router.get("/memory/{memory_id}")
async def get_memory(memory_id: str):
    """越权访问返回 404，避免探测他人记忆存在性。"""
    logger.debug("长期记忆详情 API 入口", memory_id=memory_id)
    from src.memory.long_term_store import get_long_term_memory_store

    store = await get_long_term_memory_store()
    row = await store.get_memory(memory_id)
    if row is None:
        logger.warning("长期记忆详情未命中", memory_id=memory_id)
        raise HTTPException(404, "记忆不存在")
    logger.info("长期记忆详情 API 完成", memory_id=memory_id)
    return _serialize_memory(row)


# 方法作用：删除当前登录用户的一条长期记忆。
# Args: memory_id - 记忆 ID。
# Returns: 删除结果；不存在或非本人时 404。
@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str):
    """PostgreSQL 与向量库双后端一致删除。"""
    logger.debug("长期记忆删除 API 入口", memory_id=memory_id)
    from src.memory.long_term_store import get_long_term_memory_store

    store = await get_long_term_memory_store()
    if not await store.delete_memory(memory_id):
        raise HTTPException(404, "记忆不存在")
    result = {"status": "ok", "id": memory_id}
    logger.info("长期记忆删除 API 完成", memory_id=memory_id)
    return result
