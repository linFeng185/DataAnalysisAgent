"""11.2 Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=20_000, examples=["过去7天各品类销售额"])
    session_id: str = Field(default="", max_length=128)
    datasource: str = Field(default="", max_length=64)
    datasources: list[str] = Field(default_factory=list, max_length=20)
    model_id: str = Field(default="", max_length=128)
    enabled_skill_ids: list[str] = Field(default_factory=list, max_length=20)
    stream: bool = False


class ChartAdjustRequest(BaseModel):
    """使用已有查询结果重生成图表的请求。"""

    rows: list[dict[str, Any]] = Field(..., min_length=1, max_length=500)
    instruction: str = Field(..., min_length=1, max_length=100)


class SkillRegistryInstallRequest(BaseModel):
    """从中心 Registry 安装指定审核版本的请求。"""

    version: str = Field(..., min_length=1, max_length=64)
    scope: str = Field(default="private", pattern="^(system|tenant|private)$")


class AutomationScheduleCreateRequest(BaseModel):
    """创建主动洞察或定时报告任务的请求。"""

    name: str = Field(..., min_length=1, max_length=128)
    kind: Literal["insight", "report"]
    datasource: str = Field(..., min_length=1, max_length=64)
    sql: str = Field(..., min_length=1, max_length=20_000)
    frequency: Literal["hourly", "daily", "weekly", "monthly"]
    threshold_pct: float = Field(default=10.0, ge=0, le=10_000)
    channels: list[Literal["in_app", "email", "feishu", "slack"]] = Field(
        default_factory=lambda: ["in_app"],
        min_length=1,
        max_length=4,
    )
    recipient_email: str = Field(default="", max_length=320)


class ChatResponse(BaseModel):
    success: bool
    status: str = "success"
    source: str = "sql_query"
    session_id: str = ""
    user_query: str = ""
    sql: str = ""
    sql_statements: list[dict] = Field(default_factory=list)
    data: list[dict] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    analysis: dict = Field(default_factory=dict)
    chart: dict = Field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""


class ErrorResponse(BaseModel):
    success: bool = False
    error_code: str
    error_message: str
    detail: Any = None


class DataSourceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    dialect: str = Field(..., pattern="^(clickhouse|mysql|postgres|oracle|mssql|sqlite)$")
    host: str = "localhost"
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    version: str = ""
    description: str = ""
    db_schema: str = Field(default="", alias="schema")
    tablespace: str = ""
    service_name: str = ""
    instance: str = ""
    file_path: str = ""
    tags: list[str] = Field(default_factory=list)
    extra_params: dict[str, Any] = Field(default_factory=dict)


class DataSourceUpdateRequest(BaseModel):
    """数据源更新请求，空密码表示沿用原凭证。"""

    dialect: str = Field(..., pattern="^(clickhouse|mysql|postgres|oracle|mssql|sqlite)$")
    host: str = "localhost"
    port: int = 0
    database: str = ""
    username: str = ""
    password: str | None = None
    version: str = ""
    description: str = ""
    db_schema: str = Field(default="", alias="schema")
    tablespace: str = ""
    service_name: str = ""
    instance: str = ""
    file_path: str = ""
    tags: list[str] = Field(default_factory=list)
    extra_params: dict[str, Any] = Field(default_factory=dict)


class ColumnCommentRequest(BaseModel):
    comment: str = Field(..., min_length=1, max_length=500)


class ModelTestRequest(BaseModel):
    """模型连通性测试请求。"""

    model_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )


class DataSourceInfo(BaseModel):
    name: str
    dialect: str
    version: str = ""
    mode: str
    host: str
    port: int = 0
    database: str = ""
    username: str = ""
    description: str = ""
    connected: bool = False


class TableInfo(BaseModel):
    name: str
    description: str = ""
    columns: list[dict] = Field(default_factory=list)
    row_count_estimate: int = 0


class MCPServerCreate(BaseModel):
    """创建 system/tenant/private MCP Server 的请求。"""

    name: str = Field(..., min_length=1, max_length=64)
    scope: str = Field(default="private", pattern="^(system|tenant|private)$")
    transport: str = "sse"
    command: str = ""
    args: str = ""
    url: str = ""
    env_vars: dict = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True


class KnowledgeTagCreateRequest(BaseModel):
    """创建个人或全局知识标签的请求。"""

    name: str = Field(..., min_length=1, max_length=128)
    tag_group: str = Field(default="custom", min_length=1, max_length=32)
    description: str = Field(default="", max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeTagStatusRequest(BaseModel):
    """启用或停用知识标签的请求。"""

    is_active: bool


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    llm_available: bool = False
    uptime_seconds: float = 0
