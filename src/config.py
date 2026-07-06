"""项目配置管理 — 基于 pydantic-settings，从 .env / 环境变量加载。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 (src/config.py → src/ → 项目根)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"

# 如果 .env 不存在，从 .env.example 复制
if not _ENV_FILE.exists() and _ENV_EXAMPLE.exists():
    shutil.copy(_ENV_EXAMPLE, _ENV_FILE)
    print(f"[init] 自动创建 .env 从 {_ENV_EXAMPLE}，请编辑 .env 填入真实配置")

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
    embedding_model_path: str = ""  # all-MiniLM-L6-v2 模型目录路径，必须配置

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- 会话上下文裁剪 ----
    # DeepSeek V4 有 1M 上下文，默认 50K 足够 20+ 轮完整对话，且不浪费 Token 成本
    context_max_tokens: int = 50000   # Token 预算上限（DeepSeek V4: 1M, 本地模型: 酌情降低）
    context_hot_turns: int = 5        # 热窗口: 最近 N 轮完整保留
    context_warm_turns: int = 20      # 温窗口: N+1~M 轮压缩为摘要
    context_summary_model: str = ""   # 压缩摘要专用模型，空则复用 cheap_llm

    # ---- 重试 ----
    max_retry_count: int = 3

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
    """内置 Skills 目录（始终加载，不会被环境变量覆盖）。"""
    extra_skills_dirs: str = ""
    """额外的 Skills 搜索目录，多个路径以分号分隔。上传时优先写入第一个额外目录。"""

    # ---- 业务文档 ----
    metrics_docs_dir: str = "docs/metrics"


def get_settings() -> Settings:
    """Settings 单例工厂。"""
    return Settings()
