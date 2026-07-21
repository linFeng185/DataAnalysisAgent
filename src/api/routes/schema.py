"""Schema 查询与标注路由。"""

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



def _schema_manager():
    """获取全局 SchemaManager 实例。

    Returns:
        SchemaManager 单例。
    """
    from src.knowledge.schema_manager import get_schema_manager
    return get_schema_manager()


# ---- Schema (11.1.3-6) ----

@router.get("/schema/tables")
async def list_tables(
    datasource: str = Query(default="demo"),
    page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
    search: str = Query(default=""),
) -> dict:
    try:
        import src.api.routes as routes_package

        ds = await routes_package._registry().resolve(datasource)
    except DataSourceNotFoundError:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    if ds.schema is None:
        logger.info("Schema 路由触发延迟内省", datasource=datasource)
        ds.schema = await routes_package._schema_manager().get_or_fetch_schema(datasource)
    tables = []
    for t in (ds.schema.tables if ds.schema else []):
        if search and search.lower() not in t.name.lower():
            continue
        tables.append(TableInfo(name=t.name, description=t.description,
            columns=[{"name": c.name, "type": c.type, "comment": c.comment,
                      "is_indexed": c.is_indexed, "is_primary_key": c.is_primary_key}
                     for c in t.columns],
            row_count_estimate=t.row_count_estimate))
    total = len(tables)
    start = (page - 1) * page_size
    return {"tables": tables[start:start+page_size], "datasource": datasource,
            "total": total, "page": page, "page_size": page_size}


@router.get("/schema/tables/{table_name}")
async def get_table(table_name: str, datasource: str = Query(default="demo")):
    try:
        import src.api.routes as routes_package

        ds = await routes_package._registry().resolve(datasource)
    except DataSourceNotFoundError:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    if ds.schema is None:
        logger.info("表详情路由触发延迟内省", datasource=datasource)
        ds.schema = await routes_package._schema_manager().get_or_fetch_schema(datasource)
    for t in (ds.schema.tables if ds.schema else []):
        if t.name == table_name:
            return TableInfo(name=t.name, description=t.description,
                columns=[{"name": c.name, "type": c.type, "comment": c.comment,
                          "is_nullable": c.is_nullable, "is_primary_key": c.is_primary_key}
                         for c in t.columns],
                row_count_estimate=t.row_count_estimate)
    raise HTTPException(404, f"表 '{table_name}' 未找到")


@router.post("/schema/refresh")
async def refresh_schema(datasource: str = Query(default="demo")):
    """验证数据源后真实刷新 Schema 缓存。

    Args:
        datasource: 数据源名称。

    Returns:
        刷新状态和表数量。
    """
    logger.debug("Schema 刷新路由入口", datasource=datasource)
    try:
        import src.api.routes as routes_package

        await routes_package._registry().resolve(datasource)
    except DataSourceNotFoundError:
        logger.warning("Schema 刷新数据源不存在", datasource=datasource)
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    snapshot = await routes_package._schema_manager().refresh(datasource)
    result = {
        "status": "ok",
        "message": "刷新完成",
        "datasource": datasource,
        "table_count": len(snapshot.tables),
    }
    logger.info("Schema 刷新路由完成", datasource=datasource, table_count=result["table_count"])
    return result


@router.put("/schema/tables/{table_name}/columns/{column_name}/comment")
async def update_column_comment(
    table_name: str, column_name: str, req: ColumnCommentRequest,
    datasource: str = Query(default="demo"),
):
    """更新指定数据源中的字段备注。

    Args:
        table_name: 表名。
        column_name: 字段名。
        req: 备注请求体。
        datasource: 数据源名称。

    Returns:
        更新后的字段备注摘要。
    """
    logger.debug("字段备注路由入口", datasource=datasource, table=table_name, column=column_name)
    try:
        import src.api.routes as routes_package

        await routes_package._registry().resolve(datasource)
    except DataSourceNotFoundError:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    updated = await routes_package._schema_manager().update_column_comment(
        datasource, table_name, column_name, req.comment,
    )
    if not updated:
        raise HTTPException(404, f"字段 '{table_name}.{column_name}' 未找到")
    logger.info("字段备注路由完成", datasource=datasource, table=table_name, column=column_name)
    return {"status": "ok", "table": table_name, "column": column_name, "comment": req.comment}
