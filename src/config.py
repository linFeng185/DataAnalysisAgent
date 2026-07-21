"""项目配置管理 — 基于 pydantic-settings，从 .env / 环境变量加载。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 (src/config.py → src/ → 项目根)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"

logger = logging.getLogger(__name__)

# 配置模块只读取文件，不在 import 阶段修改工作区。
if not _ENV_FILE.exists() and _ENV_EXAMPLE.exists():
    logger.warning(".env 不存在，请参考 %s 创建本地配置", _ENV_EXAMPLE)

# 将 .env 中所有键值注入 os.environ，确保 CredentialManager.resolve_env_ref 能解析 ${VAR} 占位符
load_dotenv(_ENV_FILE)


class Settings(BaseSettings):
    """全局配置，所有字段可从环境变量或 .env 文件覆盖。"""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 运行环境 ----
    env: Literal["dev", "prod", "test"] = "dev"
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
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/data_agent"
    database_readonly_url: str = ""  # 只读账号，配了就用它执行用户 SQL
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
    jwt_secret: str = ""
    jwt_access_token_expire_hours: int = 24
    jwt_refresh_token_expire_days: int = 7
    admin_api_key: str = ""                  # 管理端点 API Key，空=不启用
    credential_encryption_key: str = ""

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

    # ---- LangSmith ----
    langsmith_api_key: str = ""
    langsmith_project: str = "data-analysis-agent"

    # ---- MCP ----
    mcp_config_path: str = "config/mcp_servers.yaml"
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


def get_settings() -> Settings:
    """Settings 单例工厂。"""
    return Settings()


def validate_production_settings(settings: Settings) -> None:
    """校验生产环境必须具备的认证、凭证和只读数据库配置。

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
    if not settings.multi_tenant:
        errors.append("MULTI_TENANT 必须为 true")
    if len(settings.jwt_secret) < 32:
        errors.append("JWT_SECRET 至少需要 32 字符")
    if len(settings.admin_api_key) < 32:
        errors.append("ADMIN_API_KEY 至少需要 32 字符")
    if len(settings.credential_encryption_key) < 32:
        errors.append("CREDENTIAL_ENCRYPTION_KEY 至少需要 32 字符")
    if not settings.database_readonly_url:
        errors.append("DATABASE_READONLY_URL 必须配置")
    if not settings.run_migrations_on_startup:
        errors.append("RUN_MIGRATIONS_ON_STARTUP 必须为 true")

    if errors:
        message = "生产配置无效: " + "; ".join(errors)
        logger.error("生产配置校验失败", extra={"errors": errors})
        raise ValueError(message)

    logger.info("生产配置校验通过")
