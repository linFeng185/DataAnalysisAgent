"""项目配置管理 — 基于 pydantic-settings，从 .env / 环境变量加载。"""

from __future__ import annotations

import logging
import os
import ipaddress
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings import YamlConfigSettingsSource

# 项目根目录 (src/config.py → src/ → 项目根)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"
_DEFAULT_APP_CONFIG_FILE = _PROJECT_ROOT / "config" / "app.yaml"

logger = logging.getLogger(__name__)

# 配置模块只读取文件，不在 import 阶段修改工作区。
if not _ENV_FILE.exists() and _ENV_EXAMPLE.exists():
    logger.warning(".env 不存在，请参考 %s 创建本地配置", _ENV_EXAMPLE)

# 将 .env 中所有键值注入 os.environ，确保 CredentialManager.resolve_env_ref 能解析 ${VAR} 占位符
load_dotenv(_ENV_FILE)


class ApiAccessRouteConfig(BaseModel):
    """单条 YAML API 访问基线策略。"""

    id: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]+$")
    path: str = Field(..., min_length=1, max_length=512, pattern=r"^/")
    path_type: Literal["exact", "template"] = "exact"
    methods: list[str] = Field(min_length=1)
    auth: Literal["public", "optional", "jwt", "jwt_or_admin_key", "super_admin"]
    access_log: Literal["standard", "security", "audit", "none"] = "standard"
    description: str = Field(default="", max_length=500)

    # 方法作用：把 HTTP 方法规范化为大写并拒绝空值和重复项。
    # Args: methods - YAML 中声明的方法列表。
    # Returns: 去重后的大写 HTTP 方法列表。
    @field_validator("methods")
    @classmethod
    def normalize_methods(cls, methods: list[str]) -> list[str]:
        normalized: list[str] = []
        for method in methods:
            value = str(method).strip().upper()
            if not value or not value.replace("-", "").isalpha():
                raise ValueError("HTTP 方法格式无效")
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("至少需要一个 HTTP 方法")
        return normalized


# 方法作用：返回系统不可缺失的公开、可选认证和管理 Key 启动策略。
# Args: 无。
# Returns: 默认 API 访问基线策略列表。
def _default_api_access_policies() -> list[ApiAccessRouteConfig]:
    return [
        ApiAccessRouteConfig(
            id="health", path="/api/v1/health", methods=["GET"], auth="public",
            access_log="none", description="健康检查",
        ),
        ApiAccessRouteConfig(
            id="login", path="/api/v1/auth/login", methods=["POST"], auth="public",
            access_log="security", description="登录",
        ),
        ApiAccessRouteConfig(
            id="register", path="/api/v1/auth/register", methods=["POST"], auth="public",
            access_log="security", description="公开注册",
        ),
        ApiAccessRouteConfig(
            id="logout", path="/api/v1/auth/logout", methods=["POST"], auth="public",
            access_log="standard", description="退出登录",
        ),
        ApiAccessRouteConfig(
            id="auth_probe", path="/api/v1/auth/me", methods=["GET"], auth="optional",
            access_log="standard", description="认证状态探测",
        ),
        ApiAccessRouteConfig(
            id="datasource_create", path="/api/v1/datasources", methods=["POST"],
            auth="jwt_or_admin_key", access_log="audit", description="创建数据源",
        ),
        ApiAccessRouteConfig(
            id="datasource_delete", path="/api/v1/datasources/{name}", path_type="template",
            methods=["DELETE"], auth="jwt_or_admin_key", access_log="audit",
            description="删除数据源",
        ),
        ApiAccessRouteConfig(
            id="schema_refresh", path="/api/v1/schema/refresh", methods=["POST"],
            auth="jwt_or_admin_key", access_log="audit", description="刷新 Schema",
        ),
        ApiAccessRouteConfig(
            id="schema_column_comment",
            path="/api/v1/schema/tables/{table}/columns/{column}/comment",
            path_type="template", methods=["PUT"], auth="jwt_or_admin_key",
            access_log="audit", description="更新字段注释",
        ),
        ApiAccessRouteConfig(
            id="model_test", path="/api/v1/models/test", methods=["POST"],
            auth="jwt_or_admin_key", access_log="audit", description="模型连通性测试",
        ),
    ]


class ApiAccessConfig(BaseModel):
    """API 认证基线、访问日志和紧急 IP 策略配置。"""

    default_auth: Literal["jwt", "super_admin"] = "jwt"
    default_access_log: Literal["standard", "security", "audit", "none"] = "standard"
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    emergency_ip_deny: list[str] = Field(default_factory=list)
    bootstrap_policies: list[ApiAccessRouteConfig] = Field(
        default_factory=_default_api_access_policies,
    )

    # 方法作用：规范化并校验可信代理和紧急黑名单 CIDR。
    # Args: entries - IP 或 CIDR 字符串列表。
    # Returns: 规范化后的 CIDR 列表。
    @field_validator("trusted_proxy_cidrs", "emergency_ip_deny")
    @classmethod
    def normalize_cidrs(cls, entries: list[str]) -> list[str]:
        normalized: list[str] = []
        for entry in entries:
            network = ipaddress.ip_network(str(entry).strip(), strict=False)
            value = str(network)
            if value not in normalized:
                normalized.append(value)
        return normalized

    # 方法作用：确保启动策略编号唯一，避免数据库 IP 规则引用歧义。
    # Args: self - 已完成字段校验的配置实例。
    # Returns: 校验通过的配置实例。
    @model_validator(mode="after")
    def validate_unique_policy_ids(self) -> "ApiAccessConfig":
        ids = [policy.id for policy in self.bootstrap_policies]
        if len(ids) != len(set(ids)):
            raise ValueError("bootstrap_policies 策略编号不能重复")
        return self


class Settings(BaseSettings):
    """全局配置，环境变量覆盖 .env 和统一 YAML 配置。"""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 方法作用：按显式参数、环境变量、.env、统一 YAML 和默认值顺序加载配置。
    # Args: settings_cls - Settings 类型；init_settings/env_settings/dotenv_settings/file_secret_settings - Pydantic 配置源。
    # Returns: 按优先级排列的配置源元组。
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        logger.debug("统一配置源装配入口")
        config_path = Path(os.getenv("APP_CONFIG_PATH", str(_DEFAULT_APP_CONFIG_FILE)))
        yaml_settings = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=config_path,
            yaml_file_encoding="utf-8",
        )
        result = (
            init_settings,
            env_settings,
            dotenv_settings,
            yaml_settings,
            file_secret_settings,
        )
        logger.info("统一配置源装配完成", extra={"config_path": str(config_path)})
        return result

    # ---- 运行环境 ----
    env: Literal["dev", "prod", "test"] = "prod"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_file: str = "logs/app.log"

    # ---- LLM ----
    llm_provider: Literal["openai", "anthropic"] = "openai"
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_timeout: int = 60
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""

    cheap_llm_model: str = "gpt-4o-mini"
    local_llm_model: str = ""
    local_llm_base_url: str = ""
    local_llm_api_key: str = "local"
    local_llm_timeout: int = 15
    llm_remote_tasks: str = "generate_sql"
    llm_allow_remote_fallback: bool = False

    # ---- 数据库 (智能体自身的状态存储) ----
    database_url: str = ""
    run_migrations_on_startup: bool = True

    # ---- 向量存储 ----
    vector_store_type: str = "chroma"
    vector_store_abstract_enabled: bool = True
    milvus_uri: str = ""

    # ---- ChromaDB ----
    chroma_persist_dir: str = "./chroma_data"
    chroma_collection_name: str = "data_agent_knowledge"
    embedding_model_path: str = ""  # all-MiniLM-L6-v2 模型目录路径，必须配置

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- 数据源内容缓存 ----
    datasource_cache_backend: Literal["local", "redis"] = "local"
    datasource_cache_dir: str = "./data/cache/datasource"
    datasource_cache_ttl_seconds: int = 7 * 24 * 60 * 60
    datasource_cache_redis_prefix: str = "data-agent:datasource-cache"

    # ---- 多租户与认证 ----
    multi_tenant: bool = False               # 是否启用多租户
    registration_enabled: bool = False
    super_admin_username: str = "admin"
    super_admin_password: str = ""
    jwt_secret: str = ""
    jwt_access_token_expire_hours: int = 24
    jwt_refresh_token_expire_days: int = 7
    admin_api_key: str = ""                  # 管理端点 API Key，空=不启用
    credential_encryption_key: str = ""

    # ---- API 浏览器安全 ----
    cors_allowed_origins: str = ""
    security_hsts_seconds: int = 31_536_000
    api_access: ApiAccessConfig = Field(default_factory=ApiAccessConfig)

    # ---- LLM 降级 ----
    llm_fallback_chain: str = ""             # 降级链 "gpt-4o,claude-sonnet-4-6"

    # ---- 会话上下文裁剪 ----
    # DeepSeek V4 有 1M 上下文，默认 50K 足够 20+ 轮完整对话，且不浪费 Token 成本
    context_max_tokens: int = 50000   # Token 预算上限（DeepSeek V4: 1M, 本地模型: 酌情降低）
    context_hot_turns: int = 5        # 热窗口: 最近 N 轮完整保留
    context_warm_turns: int = 20      # 温窗口: N+1~M 轮压缩为摘要
    context_summary_model: str = ""   # 压缩摘要专用模型，空则复用 cheap_llm

    # ---- 分析器 ----
    analysis_data_max_chars: int = 50000  # 分析数据最多投喂 LLM 的字符数，超限按比例均匀抽取

    # ---- 重试 ----
    max_retry_count: int = 3

    # ---- 限流 ----
    max_queries_per_hour: int = 100
    login_max_per_hour: int = 20
    registration_max_per_hour: int = 10
    login_lockout_threshold: int = 5
    login_lockout_minutes: int = 15
    max_query_chars: int = 8_000
    max_datasources_per_query: int = 5
    max_scan_rows: int = 10_000_000
    max_execution_time: int = 30
    max_result_rows: int = 100_000
    max_stats_rows: int = 500_000
    max_upload_bytes: int = 20 * 1024 * 1024
    max_upload_files: int = 20
    max_upload_total_bytes: int = 100 * 1024 * 1024

    # ---- SQL 安全 ----
    explain_skip_dialects: list[str] = ["snowflake"]
    datasource_host_allowlist: str = ""
    """允许访问私网的可信数据库主机、IP 或 CIDR，逗号分隔。"""

    # ---- LangSmith ----
    langsmith_api_key: str = ""
    langsmith_project: str = "data-analysis-agent"

    # ---- MCP ----
    mcp_config_path: str = "config/app.yaml"
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mcp_remote_host_allowlist: str = ""
    """数据库受管 SSE MCP 的精确主机 allowlist，逗号分隔。"""

    # ---- Skills ----
    skills_dir: str = "skills"
    """内置 Skills 目录（始终加载，不会被环境变量覆盖）。"""
    extra_skills_dirs: str = ""
    """额外的 Skills 搜索目录，多个路径以分号分隔。上传时优先写入第一个额外目录。"""
    managed_skills_dir: str = "data/skills"
    """租户级和个人级 Skill 的受管根目录。"""

    # ---- 业务文档 ----
    metrics_docs_dir: str = "docs/metrics"
    system_knowledge_dirs: str = ""
    """系统知识只读目录，多个路径使用分号分隔。"""


# 方法作用：从环境变量和项目配置文件构造当前应用设置。
# Args: 无。
# Returns: 当前应用 Settings 实例。
def get_settings() -> Settings:
    """返回当前 AppContext 持有的唯一配置实例。"""
    logger.debug("获取应用配置入口")
    try:
        from src.app_context import get_app_context

        result = cast(Settings, get_app_context().settings)
        logger.debug(
            "获取应用配置完成",
            extra={
                "env": getattr(result, "env", "unknown"),
                "source": "app_context",
                "settings_id": id(result),
            },
        )
        return result
    except Exception:
        logger.error("获取应用配置失败", exc_info=True)
        raise


# 方法作用：校验生产环境必须具备的认证、密钥和数据库安全配置。
# Args: settings - 待校验的应用设置。
# Returns: 校验通过时无返回值，失败时抛出 ValueError。
def validate_production_settings(settings: Settings) -> None:
    """校验生产环境必须具备的认证、凭证和状态数据库配置。

    Args:
        settings: 待校验的应用配置。

    Returns:
        校验通过时返回 None。

    Raises:
        ValueError: 生产配置缺失或密钥强度不足。
    """
    logger.debug("生产配置校验入口", extra={"env": settings.env})
    if settings.env != "prod":
        logger.info("非生产环境跳过强制安全配置校验", extra={"env": settings.env})
        return

    errors: list[str] = []
    if len(settings.jwt_secret) < 32:
        errors.append("JWT_SECRET 至少需要 32 字符")
    if len(settings.credential_encryption_key) < 32:
        errors.append("CREDENTIAL_ENCRYPTION_KEY 至少需要 32 字符")
    cors_origins = {
        origin.strip()
        for origin in settings.cors_allowed_origins.split(",")
        if origin.strip()
    }
    if "*" in cors_origins:
        errors.append("CORS_ALLOWED_ORIGINS 生产环境禁止使用通配符")
    try:
        parsed_database_url = urlsplit(settings.database_url)
        database_user = unquote(parsed_database_url.username or "")
        database_password = unquote(parsed_database_url.password or "")
        if not parsed_database_url.hostname or not database_user or not database_password:
            errors.append("DATABASE_URL 必须包含完整连接地址和凭证")
        elif database_user == "postgres" and database_password == "postgres":
            errors.append("DATABASE_URL 禁止使用 postgres/postgres 默认凭证")
    except ValueError as exc:
        logger.error("生产 DATABASE_URL 解析失败", exc_info=True)
        errors.append(f"DATABASE_URL 格式无效: {exc}")
    if not settings.run_migrations_on_startup:
        errors.append("RUN_MIGRATIONS_ON_STARTUP 必须为 true")

    if errors:
        message = "生产配置无效: " + "; ".join(errors)
        logger.error("生产配置校验失败", extra={"errors": errors})
        raise ValueError(message)

    logger.info("生产配置校验通过")
