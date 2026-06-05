"""骨架冒烟测试 — 验证所有基础设施模块可正常 import 和实例化。"""

from __future__ import annotations

import pytest


class TestConfig:
    """配置管理 (1.1.3, 1.2.1)。"""

    def test_settings_instantiate(self):
        from src.config import Settings

        s = Settings()
        assert s.env == "dev"
        assert s.llm_provider == "openai"
        assert s.max_queries_per_hour == 100

    def test_get_settings(self):
        from src.config import get_settings

        s = get_settings()
        assert s.env == "dev"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ENV", "prod")
        from src.config import Settings

        s = Settings()
        assert s.env == "prod"


class TestExceptions:
    """异常体系 (1.1.7, 1.3.1-1.3.6)。"""

    def test_data_source_not_found(self):
        from src.exceptions import DataSourceNotFoundError

        exc = DataSourceNotFoundError("ch_prod")
        assert "ch_prod" in str(exc)
        assert exc.datasource == "ch_prod"

    def test_sql_validation_error(self):
        from src.exceptions import SQLValidationError

        exc = SQLValidationError(
            errors=[{"type": "syntax_error", "message": "unexpected token"}],
        )
        assert len(exc.errors) == 1

    def test_sql_security_error(self):
        from src.exceptions import SQLSecurityError

        exc = SQLSecurityError(reason="DROP 操作", violated_operation="DROP")
        assert "DROP" in exc.reason

    def test_execution_error(self):
        from src.exceptions import ExecutionError

        exc = ExecutionError(message="连接超时", retry_count=2, sql="SELECT 1")
        assert exc.retry_count == 2

    def test_rate_limit_error(self):
        from src.exceptions import RateLimitError

        exc = RateLimitError(user_id="u42", limit=100)
        assert "100" in str(exc)

    def test_knowledge_not_found(self):
        from src.exceptions import KnowledgeNotFoundError

        exc = KnowledgeNotFoundError("GMV")
        assert exc.query == "GMV"

    def test_mcp_connection_error(self):
        from src.exceptions import MCPConnectionError

        exc = MCPConnectionError(server_name="f", detail="exit")
        assert "f" in str(exc)

    def test_hierarchy(self):
        from src.exceptions import (
            DataAnalysisAgentError,
            DataSourceNotFoundError,
            ExecutionError,
        )

        assert issubclass(DataSourceNotFoundError, DataAnalysisAgentError)
        assert issubclass(ExecutionError, DataAnalysisAgentError)


class TestLogging:
    """日志配置 (1.1.6)。"""

    def test_setup_logging(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        from src.logging_config import setup_logging

        setup_logging()

    def test_get_logger(self):
        from src.logging_config import get_logger

        log = get_logger("test")
        assert log is not None


class TestMain:
    """FastAPI 入口 (1.1.2)。"""

    def test_create_app(self):
        from src.main import create_app

        app = create_app()
        assert app.title == "Data Analysis Agent"
        assert app.version == "0.1.0"
