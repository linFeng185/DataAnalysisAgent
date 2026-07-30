"""LLM 治理增强迁移契约测试。"""

from __future__ import annotations

from pathlib import Path


class TestLLMGovernanceMigration:
    """覆盖功能 10.1.13-10.1.15 的数据库升级契约。"""

    # 方法作用：验证迁移增加厂商动态能力表单并替换过期 DeepSeek 种子。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_migration_adds_schema_and_current_deepseek_models(self) -> None:
        """已有数据库必须迁移到 V4 Pro/Flash，不能继续保留 deepseek-chat。"""
        # Arrange
        path = Path("migrations/014_llm_catalog_reasoning.sql")

        # Act
        source = path.read_text(encoding="utf-8")
        normalized = source.lower()

        # Assert
        assert "add column if not exists capability_schema jsonb" in normalized
        assert "deepseek-v4-pro" in source
        assert "deepseek-v4-flash" in source
        assert "https://api.deepseek.com" in source
        assert "delete from llm_model_catalog" in normalized
        assert "deepseek-chat" in source
        assert "tenant_llm_connection_models" in source
        assert "tenant_llm_defaults" in source

    # 方法作用：验证迁移中的 DeepSeek 能力声明覆盖推理和工具调用协议。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_deepseek_seed_declares_reasoning_contract(self) -> None:
        """两个模型都必须声明 high/max、reasoning_content 和 function calling。"""
        # Arrange
        source = Path("migrations/014_llm_catalog_reasoning.sql").read_text(
            encoding="utf-8",
        )

        # Act / Assert
        assert '"reasoning_efforts":["high","max"]' in source
        assert '"reasoning_content_in_response":true' in source
        assert '"function_calling":true' in source
        assert source.count('"context_window":1000000') >= 2
