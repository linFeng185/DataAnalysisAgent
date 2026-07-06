"""11.2 Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str = Field(..., examples=["过去7天各品类销售额"])
    session_id: str = ""
    datasource: str = "demo"
    stream: bool = False


class ChatResponse(BaseModel):
    success: bool
    session_id: str = ""
    user_query: str = ""
    sql: str = ""
    data: list[dict] = []
    analysis: dict = {}
    chart: dict = {}


class ErrorResponse(BaseModel):
    success: bool = False
    error_code: str
    error_message: str
    detail: Any = None


class DataSourceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    dialect: str = Field(..., pattern="^(clickhouse|mysql|postgres|presto|hive|oracle|mssql|sqlite)$")
    host: str = "localhost"
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    version: str = ""
    description: str = ""
    schema: str = ""
    tablespace: str = ""
    service_name: str = ""
    instance: str = ""
    file_path: str = ""
    tags: list[str] = []
    extra_params: dict[str, Any] = {}


class ColumnCommentRequest(BaseModel):
    comment: str = Field(..., min_length=1, max_length=500)


class DataSourceInfo(BaseModel):
    name: str
    dialect: str
    version: str = ""
    mode: str
    host: str
    database: str = ""
    description: str = ""
    connected: bool = False


class TableInfo(BaseModel):
    name: str
    description: str = ""
    columns: list[dict] = []
    row_count_estimate: int = 0


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    llm_available: bool = False
    uptime_seconds: float = 0
