"""项目配置管理 — 基于 pydantic-settings，从 .env / 环境变量加载。"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置，所有字段可从环境变量或 .env 文件覆盖。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 运行环境 ----
    env: Literal["dev", "prod", "test"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

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

    # ---- 数据库 (智能体自身的状态存储) ----
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/data_agent"

    # ---- ChromaDB ----
    chroma_persist_dir: str = "./chroma_data"
    chroma_collection_name: str = "data_agent_knowledge"

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- 限流 ----
    max_queries_per_hour: int = 100
    max_scan_rows: int = 10_000_000
    max_execution_time: int = 30
    max_result_rows: int = 100_000
    max_stats_rows: int = 500_000

    # ---- SQL 安全 ----
    explain_skip_dialects: list[str] = ["snowflake"]

    # ---- LangSmith ----
    langsmith_api_key: str = ""
    langsmith_project: str = "data-analysis-agent"

    # ---- MCP ----
    mcp_config_path: str = "config/mcp_servers.yaml"

    # ---- Skills ----
    skills_dir: str = "skills"

    # ---- 业务文档 ----
    metrics_docs_dir: str = "docs/metrics"


def get_settings() -> Settings:
    """Settings 单例工厂。"""
    return Settings()
