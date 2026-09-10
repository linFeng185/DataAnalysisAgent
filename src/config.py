"""项目配置管理 — 基于 pydantic-settings，从 .env / 环境变量加载。"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

# 项目根目录 (src/config.py → src/ → 项目根)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"
_DEFAULT_APP_CONFIG_FILE = _PROJECT_ROOT / "config" / "app.yaml"
_DEFAULT_APP_CONFIG_EXAMPLE_FILE = _PROJECT_ROOT / "config" / "app.example.yaml"

logger = logging.getLogger(__name__)

# 配置模块只读取文件，不在 import 阶段修改工作区。
if not _ENV_FILE.exists() and _ENV_EXAMPLE.exists():
    logger.warning(".env 不存在，请参考 %s 创建本地配置", _ENV_EXAMPLE)

# 记录启动时由 .env 注入的键，支持显式 _env_file=None 时准确排除 dotenv 来源。
_environment_before_dotenv = set(os.environ)
load_dotenv(_ENV_FILE)
_DOTENV_INJECTED_VALUES = {
    key: os.environ[key]
    for key in set(os.environ) - _environment_before_dotenv
}


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
            id="metrics", path="/api/v1/metrics", methods=["GET"], auth="public",
            access_log="none", description="Prometheus 指标抓取",
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
            id="report_shared_get", path="/api/v1/report-workbench/shared/reports/{token}",
            path_type="template", methods=["GET"], auth="optional", access_log="audit",
            description="公开报表快照访问",
        ),
        ApiAccessRouteConfig(
            id="report_shared_unlock", path="/api/v1/report-workbench/shared/reports/{token}/unlock",
            path_type="template", methods=["POST"], auth="public", access_log="audit",
            description="公开报表授权码解锁",
        ),
        ApiAccessRouteConfig(
            id="report_shared_drilldown", path="/api/v1/report-workbench/shared/reports/{token}/drilldowns/{drilldown_id}",
            path_type="template", methods=["POST"], auth="optional", access_log="audit",
            description="公开报表快照明细钻取",
        ),
        ApiAccessRouteConfig(
            id="report_permissions_me", path="/api/v1/report-workbench/permissions/me",
            methods=["GET"], auth="jwt", access_log="standard", description="当前用户报表能力",
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
        configured_path = os.getenv("APP_CONFIG_PATH", "").strip()
        config_path = (
            Path(configured_path)
            if configured_path
            else (
                _DEFAULT_APP_CONFIG_FILE
                if _DEFAULT_APP_CONFIG_FILE.exists()
                else _DEFAULT_APP_CONFIG_EXAMPLE_FILE
            )
        )
        yaml_settings = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=config_path,
            yaml_file_encoding="utf-8",
        )
        # Pydantic 的 env_settings 会看到 import 阶段注入的 .env；显式禁用 dotenv 时将其移除，
        # 但保留测试或部署运行期间后来设置的真实环境变量覆盖。
        if getattr(dotenv_settings, "env_file", None) is None:
            env_vars = getattr(env_settings, "env_vars", {})
            for key, value in _DOTENV_INJECTED_VALUES.items():
                normalized_key = key.lower()
                if env_vars.get(normalized_key) == value:
                    env_vars.pop(normalized_key, None)
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
    server_reload_enabled: bool = True

    # ---- LLM ----
    llm_provider: Literal["openai", "anthropic"] = "openai"
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_timeout: int = 60
    analysis_llm_timeout: float = Field(default=30.0, ge=0.1, le=3600.0)
    llm_stream_chunk_timeout: int = 30

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

    # ---- 记忆维护 ----
    memory_maintenance_enabled: bool = True
    memory_maintenance_interval_seconds: int = 24 * 60 * 60
    knowledge_evolution_promotion_threshold: int = Field(default=3, ge=2, le=100)

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
    schema_cache_refresh_enabled: bool = True
    schema_cache_refresh_interval_seconds: int = 60 * 60

    # ---- Skill Registry ----
    skill_registry_url: str = ""
    skill_registry_trusted_hosts: str = ""
    skill_registry_max_package_bytes: int = 10 * 1024 * 1024

    # ---- 主动洞察与定时报告 ----
    automation_enabled: bool = True
    report_scheduler_enabled: bool = True
    automation_poll_interval_seconds: int = Field(default=60, ge=10, le=3600)
    automation_smtp_host: str = ""
    automation_smtp_port: int = Field(default=587, ge=1, le=65535)
    automation_smtp_starttls: bool = True
    automation_smtp_username: str = ""
    automation_smtp_password: str = ""
    automation_email_from: str = ""
    automation_feishu_webhook_url: str = ""
    automation_slack_webhook_url: str = ""

    # ---- 报表工作台 Phase 1/2 ----
    report_workbench_enabled: bool = True
    report_default_ttl_days: int = Field(default=7, ge=1, le=90)
    report_max_ttl_days: int = Field(default=90, ge=1, le=3650)
    report_default_timezone: str = "Asia/Shanghai"
    report_max_units_per_run: int = Field(default=12, ge=1, le=12)
    report_max_queries_per_run: int = Field(default=16, ge=1, le=16)
    report_max_concurrency: int = Field(default=4, ge=1, le=100)
    report_max_page_bytes: int = Field(default=2_097_152, ge=1024, le=50_000_000)
    report_max_public_share_days: int = Field(default=30, ge=1, le=3650)
    report_public_share_enabled: bool = True
    report_retry_max_attempts: int = Field(default=3, ge=1, le=10)
    report_run_timeout_seconds: int = Field(default=900, ge=1, le=86_400)

    # ---- 多租户与认证 ----
    multi_tenant: bool = False               # 是否启用多租户
    registration_enabled: bool = False
    super_admin_username: str = "admin"
    super_admin_password: str = ""
    jwt_secret: str = ""
    jwt_access_token_expire_hours: int = 24
    jwt_refresh_token_expire_days: int = 7
    admin_api_key: str = ""                  # 管理端点 API Key，空=不启用
    credential_encryption_key: str = Field(default="", repr=False)
    credential_key_id: str = Field(default="", max_length=64)
    credential_previous_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    secret_provider: Literal["environment", "file", "vault"] = "environment"
    secret_file: str = ""
    vault_addr: str = ""
    vault_mount: str = "secret"
    vault_secret_path: str = ""
    vault_token_file: str = ""
    vault_namespace: str = ""
    vault_ca_file: str = ""
    vault_secret_version: int = Field(default=0, ge=0)
    vault_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # ---- API 浏览器安全 ----
    cors_allowed_origins: str = ""
    security_hsts_seconds: int = 31_536_000
    api_access: ApiAccessConfig = Field(default_factory=ApiAccessConfig)

    # ---- LLM 降级 ----
    llm_fallback_chain: str = ""             # 降级链 "gpt-4o,claude-sonnet-4-6"

    # ---- 会话上下文裁剪 ----
    # 0 表示不设置绝对上限，实际预算从当前模型 context_window 动态计算。
    context_max_tokens: int = Field(default=0, ge=0)
    context_default_window: int = Field(default=128_000, ge=1_024, le=10_000_000)
    context_compression_trigger_ratio: float = Field(default=0.80, gt=0.5, lt=0.95)
    context_compression_target_ratio: float = Field(default=0.55, gt=0.1, lt=0.8)
    context_hard_limit_ratio: float = Field(default=0.92, gt=0.8, lt=0.99)
    context_prompt_reserve_tokens: int = Field(default=12_000, ge=0, le=2_000_000)
    context_output_reserve_tokens: int = Field(default=8_192, ge=0, le=2_000_000)
    context_summary_reserve_tokens: int = Field(default=1_024, ge=128, le=100_000)
    context_hot_turns: int = Field(default=5, ge=1, le=100)
    context_warm_turns: int = Field(default=20, ge=0, le=1_000)
    context_summary_model: str = ""   # 压缩摘要专用模型，空则复用 cheap_llm

    # ---- 分析器 ----
    analysis_data_max_chars: int = 50000  # 分析数据最多投喂 LLM 的字符数，超限按比例均匀抽取
    attribution_min_converted_paths: int = Field(default=2, ge=1, le=10_000)
    attribution_time_decay_half_life_days: float = Field(default=7.0, gt=0, le=3_650)
    attribution_max_touchpoint_rows: int = Field(default=10_000, ge=200, le=100_000)

    # ---- 重试 ----
    max_retry_count: int = 3
    max_supplemental_query_rounds: int = Field(default=2, ge=0, le=3)

    # ---- 限流 ----
    max_queries_per_hour: int = 100
    login_max_per_hour: int = 20
    registration_max_per_hour: int = 10
    login_lockout_threshold: int = 5
    login_lockout_minutes: int = 15
    max_query_chars: int = 8_000
    max_sql_chars: int = 20_000
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
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "data-analysis-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_hide_inputs: bool = True
    langsmith_hide_outputs: bool = True
    langsmith_sampling_rate: float = Field(default=1.0, ge=0.0, le=1.0)

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
    skill_require_signatures: bool = True
    """受管和额外目录 Skill 是否必须提供可信 Ed25519 签名。"""
    skill_trusted_public_keys: str = ""
    """可信 Skill 签发者公钥 JSON 映射，值为 PEM 或 Base64 原始公钥。"""

    # ---- 业务文档 ----
    metrics_docs_dir: str = "docs/metrics"
    system_knowledge_dirs: str = ""
    """系统知识只读目录，多个路径使用分号分隔。"""


# 未绑定应用 Context 的 CLI 与应用工厂复用同一份配置，且不隐式创建 Context。
_standalone_settings: Settings | None = None


# 方法作用：优先读取当前 Context 配置，否则从环境和项目配置构造独立设置。
# Args: 无。
# Returns: 当前应用 Settings 实例。
def get_settings() -> Settings:
    """返回当前 Context 配置，未绑定时复用无副作用的独立配置实例。"""
    logger.debug("获取应用配置入口")
    from src.app_context import get_bound_app_context

    context = get_bound_app_context()
    if context is not None:
        result = cast(Settings, context.settings)
        logger.debug(
            "获取应用配置完成",
            extra={
                "env": getattr(result, "env", "unknown"),
                "source": "app_context",
                "settings_id": id(result),
            },
        )
        return result

    global _standalone_settings
    if _standalone_settings is None:
        _standalone_settings = Settings()
    logger.debug(
        "获取应用配置完成",
        extra={
            "env": _standalone_settings.env,
            "source": "standalone",
            "settings_id": id(_standalone_settings),
        },
    )
    return _standalone_settings


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
