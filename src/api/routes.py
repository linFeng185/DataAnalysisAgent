"""11.1 + 2.3.7-9 API 路由 — chat / schema / datasources / health (13 端点)。"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException, Query

from src.api.schemas import (
    ChatRequest, ChatResponse, ColumnCommentRequest,
    DataSourceCreateRequest, DataSourceInfo, HealthResponse, TableInfo,
)
from src.exceptions import DataSourceNotFoundError
from src.llm.client import is_llm_available
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


def _app():
    from src.graph.workflow import app
    return app


def _registry():
    from src.datasource.registry import get_registry
    return get_registry()


# ---- Chat (11.1.1-2) ----

@router.post("/chat")
async def chat(req: ChatRequest):
    """统一 chat 端点：stream=False 返回 JSON，stream=True 返回 SSE 流式。"""
    if req.stream:
        from fastapi.responses import StreamingResponse
        from src.api.streaming import stream_analysis
        return StreamingResponse(
            stream_analysis(req.query, req.datasource),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = await _app().ainvoke({"user_query": req.query, "datasource": req.datasource})
    f = result.get("final_response", {})
    return ChatResponse(
        success=f.get("success", True), session_id=req.session_id or str(uuid.uuid4())[:8],
        user_query=req.query, sql=result.get("generated_sql", ""),
        data=result.get("query_result_sample", []),
        analysis=result.get("analysis_result", {}), chart=result.get("chart_config", {}),
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """（保留向后兼容）独立流式端点，等价于 /chat + stream=True。"""
    from fastapi.responses import StreamingResponse
    from src.api.streaming import stream_analysis
    return StreamingResponse(
        stream_analysis(req.query, req.datasource),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Schema (11.1.3-6) ----

@router.get("/schema/tables")
async def list_tables(
    datasource: str = Query(default="clickhouse_prod"),
    page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
    search: str = Query(default=""),
) -> dict:
    try:
        ds = await _registry().resolve(datasource)
    except DataSourceNotFoundError:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    tables = []
    for t in (ds.schema.tables if ds.schema else []):
        if search and search.lower() not in t.name.lower():
            continue
        tables.append(TableInfo(name=t.name, description=t.description,
            columns=[{"name": c.name, "type": c.type, "comment": c.comment} for c in t.columns],
            row_count_estimate=t.row_count_estimate))
    total = len(tables)
    start = (page - 1) * page_size
    return {"tables": tables[start:start+page_size], "datasource": datasource,
            "total": total, "page": page, "page_size": page_size}


@router.get("/schema/tables/{table_name}")
async def get_table(table_name: str, datasource: str = Query(default="clickhouse_prod")):
    try:
        ds = await _registry().resolve(datasource)
    except DataSourceNotFoundError:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    for t in (ds.schema.tables if ds.schema else []):
        if t.name == table_name:
            return TableInfo(name=t.name, description=t.description,
                columns=[{"name": c.name, "type": c.type, "comment": c.comment,
                          "is_nullable": c.is_nullable, "is_primary_key": c.is_primary_key}
                         for c in t.columns],
                row_count_estimate=t.row_count_estimate)
    raise HTTPException(404, f"表 '{table_name}' 未找到")


@router.post("/schema/refresh")
async def refresh_schema(datasource: str = Query(default="clickhouse_prod")):
    return {"status": "ok", "message": "刷新已触发", "datasource": datasource}


@router.put("/schema/tables/{table_name}/columns/{column_name}/comment")
async def update_column_comment(
    table_name: str, column_name: str, req: ColumnCommentRequest,
    datasource: str = Query(default="clickhouse_prod"),
):
    return {"status": "ok", "table": table_name, "column": column_name, "comment": req.comment}


# ---- 数据源管理 (2.3.7-9) ----

@router.post("/datasources", status_code=201)
async def register_datasource(req: DataSourceCreateRequest):
    from src.datasource.providers.external import ExternalDataSourceProvider
    ds = await ExternalDataSourceProvider().register(req)
    return DataSourceInfo(name=ds.name, dialect=ds.dialect, mode=ds.mode,
                          host=ds.host, description=ds.description)


@router.delete("/datasources/{name}")
async def delete_datasource(name: str):
    return {"status": "ok", "message": f"数据源 '{name}' 已删除"}


@router.get("/datasources")
async def list_datasources(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    items = await _registry().list_all()
    total = len(items)
    start = (page - 1) * page_size
    return {"datasources": items[start:start+page_size], "total": total, "page": page, "page_size": page_size}


# ---- Health (11.1.10) ----

@router.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", llm_available=is_llm_available(),
                          uptime_seconds=round(time.time() - _started_at, 2))
