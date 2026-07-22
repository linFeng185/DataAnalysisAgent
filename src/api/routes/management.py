"""模型、结构化资产与健康检查路由。"""

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
    KnowledgeTagStatusRequest, MCPServerCreate, ModelTestRequest, TableInfo,
)
from src.exceptions import DataSourceNotFoundError
from src.llm.client import is_llm_available
from src.logging_config import get_logger
from src.api.routes._helpers import _app, _authorize_extension_scope, _registry

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


# ---- 模型管理 ----

@router.get("/models")
async def list_models():
    from src.llm.model_registry import get_model_registry
    from src.config import get_settings
    items = []
    for m in get_model_registry().list_all():
        items.append({"id": m.model_id, "provider": m.provider, "name": m.display_name,
                      "context_window": m.capabilities.context_window,
                      "vision": m.capabilities.vision, "reasoning": m.capabilities.reasoning})
    return {"models": items, "default": get_settings().llm_model}


@router.post("/models/test")
async def test_model(req: ModelTestRequest):
    """使用固定最小请求测试已注册模型连通性。

    Args:
        req: 已校验的模型 ID。

    Returns:
        连通状态和请求延迟。
    """
    import time as _t
    from src.llm.client import get_provider
    logger.debug("模型连通性测试入口", model_id=req.model_id)
    try:
        p = get_provider(req.model_id)
        s = _t.monotonic()
        await p.agenerate([{"role": "user", "content": "ping"}], max_tokens=1)
        result = {"ok": True, "latency_ms": round((_t.monotonic() - s) * 1000)}
        logger.info("模型连通性测试完成", model_id=req.model_id, ok=True)
        return result
    except Exception as exc:
        logger.error(
            "模型连通性测试失败",
            model_id=req.model_id,
            error=str(exc),
            exc_info=True,
        )
        return {"ok": False, "error": "模型连接测试失败"}

# ---- Knowledge / 知识库 ----


@router.post("/assets/profile")
async def profile_structured_asset(file: UploadFile = File(...)):
    """解析 CSV/Excel/Parquet 并返回列级 profile，不把原文件写入知识库。"""
    from src.config import get_settings
    from src.knowledge.structured_assets import StructuredAssetAdapter, StructuredAssetError

    logger.debug("结构化资产 profile API 入口", file_name=file.filename or "")
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")
    settings = get_settings()
    max_bytes = settings.max_upload_bytes
    try:
        content = await file.read(max_bytes + 1)
        profile = StructuredAssetAdapter(max_bytes=max_bytes).inspect_bytes(file.filename, content)
        result = profile.to_dict()
        logger.info("结构化资产 profile API 完成", file_name=file.filename, rows=profile.row_count)
        return result
    except StructuredAssetError as exc:
        logger.warning("结构化资产 profile API 拒绝", file_name=file.filename, error=str(exc))
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.error("结构化资产 profile API 失败", file_name=file.filename, error=str(exc), exc_info=True)
        raise HTTPException(500, "结构化资产解析失败") from exc


@router.post("/assets/query")
async def query_structured_asset(
    file: UploadFile = File(...),
    sql: str = Query(..., min_length=1),
    sheet_name: str | None = Query(default=None),
):
    """对上传的 CSV/Excel/Parquet 执行受控只读 SQL。"""
    from src.config import get_settings
    from src.knowledge.structured_query import StructuredQueryEngine, StructuredQueryError

    logger.debug("结构化资产查询 API 入口", file_name=file.filename or "", sql_preview=sql[:120])
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")
    settings = get_settings()
    max_bytes = settings.max_upload_bytes
    try:
        content = await file.read(max_bytes + 1)
        result = await StructuredQueryEngine(
            max_rows=settings.max_result_rows,
            max_bytes=max_bytes,
            max_scan_rows=getattr(settings, "max_scan_rows", 1_000_000),
        ).execute(file.filename, content, sql, sheet_name=sheet_name)
        logger.info("结构化资产查询 API 完成", file_name=file.filename, rows=result.row_count)
        return result.to_dict()
    except StructuredQueryError as exc:
        logger.warning("结构化资产查询 API 拒绝", file_name=file.filename, error=str(exc))
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.error("结构化资产查询 API 失败", file_name=file.filename, error=str(exc), exc_info=True)
        raise HTTPException(500, "结构化资产查询失败") from exc


@router.post("/analysis/forecast")
async def forecast_asset(payload: dict = Body(...)):
    """对已提供的时间序列行执行带回测和区间的确定性预测。"""
    from src.tools.forecasting import ForecastingError, forecast_rows

    logger.debug("预测 API 入口", payload_keys=sorted(payload.keys()))
    try:
        rows = payload.get("rows", [])
        result = forecast_rows(
            rows=rows,
            time_col=str(payload.get("time_col", "")),
            value_col=str(payload.get("value_col", "")),
            horizon=int(payload.get("horizon", 3)),
        )
        logger.info("预测 API 完成", model=result.model, horizon=len(result.predictions))
        return result.to_dict()
    except ForecastingError as exc:
        logger.warning("预测 API 输入拒绝", error=str(exc))
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.error("预测 API 失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "预测执行失败") from exc


# ---- Health (11.1.10) ----

@router.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", llm_available=is_llm_available(),
                          uptime_seconds=round(time.time() - _started_at, 2))
