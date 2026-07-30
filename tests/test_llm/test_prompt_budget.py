"""Prompt 统一总预算和优先级裁剪测试，覆盖功能 10.2.9。"""

from __future__ import annotations

import pytest


class TestPromptBudget:
    """覆盖总长度、优先级、System 动态段和非法预算边界。"""

    # 方法作用：验证真实 SQL Prompt 在多类超长上下文下仍满足统一总预算。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_prompt_total_budget_preserves_required_evidence(self) -> None:
        """用户问题、Schema 和重试错误必须保留，低优先级内容按预算裁剪。"""
        # Arrange
        from src.llm.prompt_budget import PromptSection, build_budgeted_prompt
        from src.llm.prompts import get_prompt_definition

        sections = [
            PromptSection(
                "skill",
                "## Skill 指令\n" + "质量检查" * 1000,
                priority=70,
                min_chars=100,
                max_chars=1200,
                target="system",
            ),
            PromptSection(
                "query",
                "## 用户问题\n统计全部订单并解释退款趋势" + "细节" * 3000,
                priority=100,
                min_chars=600,
                max_chars=2400,
            ),
            PromptSection(
                "retry_error",
                "## 上轮错误\nUnknown column paid_at" + "错误上下文" * 500,
                priority=98,
                min_chars=300,
                max_chars=1500,
            ),
            PromptSection(
                "schema",
                "## 数据库表结构\norders(id, paid_at, refund_amount)\n" + "字段定义" * 3000,
                priority=95,
                min_chars=2500,
                max_chars=6000,
            ),
            PromptSection(
                "knowledge",
                "## 业务知识\n" + "退款规则" * 2000,
                priority=50,
                min_chars=200,
                max_chars=2000,
            ),
            PromptSection(
                "history",
                "## 对话历史\n" + "历史查询" * 2000,
                priority=20,
                max_chars=1200,
            ),
        ]

        # Act
        prompt = build_budgeted_prompt(
            "sql.generate",
            sections,
            system_values={"dialect": "postgres", "skill_instructions": ""},
        )

        # Assert
        budget = get_prompt_definition("sql.generate").context_budget
        assert prompt.used_chars == len(prompt.system) + len(prompt.human)
        assert prompt.used_chars <= budget
        assert "统计全部订单" in prompt.human
        assert "Unknown column paid_at" in prompt.human
        assert "orders(id, paid_at, refund_amount)" in prompt.human
        assert "质量检查" in prompt.system
        assert "history" in prompt.truncated_sections

    # 方法作用：验证多个 section 的最低保留量先于优先级扩展分配。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_minimum_allocations_keep_lower_priority_section_visible(self) -> None:
        """预算足够覆盖最低值时，低优先级 section 不能被高优先级独占。"""
        # Arrange
        from src.llm.prompt_budget import PromptSection, build_budgeted_prompt
        from src.llm.prompts import PROMPT_REGISTRY, PromptDefinition, register_prompt

        definition = PromptDefinition(
            prompt_id="budget.minimums",
            version="1.0.0",
            task="test",
            template="BASE",
            context_budget=120,
            system_policy="S",
            capability_policy="C",
        )
        try:
            register_prompt(definition)
            sections = [
                PromptSection("high", "HIGH:" + "H" * 100, priority=100, min_chars=30, max_chars=80),
                PromptSection("low", "LOW:" + "L" * 100, priority=10, min_chars=20, max_chars=60),
            ]

            # Act
            prompt = build_budgeted_prompt("budget.minimums", sections)

            # Assert
            assert prompt.used_chars <= 120
            assert "HIGH:" in prompt.human
            assert "LOW:" in prompt.human
            assert set(prompt.truncated_sections) == {"high", "low"}
        finally:
            PROMPT_REGISTRY.pop("budget.minimums", None)

    # 方法作用：验证固定 System Prompt 自身超限时在模型调用前失败。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_fixed_system_prompt_over_budget_fails_fast(self) -> None:
        """安全 System Prompt 不允许通过静默截断满足错误预算配置。"""
        # Arrange
        from src.llm.prompt_budget import build_budgeted_prompt
        from src.llm.prompts import PROMPT_REGISTRY, PromptDefinition, register_prompt

        definition = PromptDefinition(
            prompt_id="budget.invalid",
            version="1.0.0",
            task="test",
            template="固定系统指令",
            context_budget=3,
        )
        try:
            register_prompt(definition)

            # Act / Assert
            with pytest.raises(ValueError, match="固定 System Prompt 已超过预算"):
                build_budgeted_prompt("budget.invalid", [])
        finally:
            PROMPT_REGISTRY.pop("budget.invalid", None)

    # 方法作用：验证项目内所有直接 LLM 调用边界均接入统一预算器。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_llm_call_boundaries_use_budget_builder(self) -> None:
        """新增或重构节点不能退回 System/Human 字符串直接拼接。"""
        # Arrange
        from pathlib import Path

        boundaries = [
            Path("src/graph/nodes/classify_intent.py"),
            Path("src/graph/nodes/decompose_query.py"),
            Path("src/graph/nodes/generate_sql.py"),
            Path("src/graph/nodes/analyze_result.py"),
            Path("src/graph/nodes/llm_answer.py"),
            Path("src/graph/nodes/mcp_agent.py"),
            Path("src/graph/nodes/multi_source.py"),
            Path("src/memory/context_builder.py"),
            Path("src/memory/session_archive.py"),
        ]

        # Act
        from src.logging_config import get_logger

        logger = get_logger(__name__)
        boundary_markers = {}
        for path in boundaries:
            source = path.read_text(encoding="utf-8")
            boundary_markers[str(path)] = [
                marker for marker in (
                    "build_budgeted_prompt(",
                    "invoke_structured(",
                    "prepare_invocation(",
                    "invoke_text(",
                )
                if marker in source
            ]
        logger.info("统一 Prompt 边界扫描完成", boundaries=boundary_markers)
        missing = [path for path, markers in boundary_markers.items() if not markers]

        # Assert
        assert missing == []

    def test_business_nodes_do_not_inline_system_messages(self) -> None:
        """业务节点不得绕过 PromptRegistry 直接构造 SystemMessage。"""
        # Arrange
        from pathlib import Path

        boundaries = [
            *Path("src/graph/nodes").glob("*.py"),
            Path("src/memory/context_builder.py"),
            Path("src/memory/session_archive.py"),
        ]
        violations: list[str] = []

        # Act
        for path in boundaries:
            source = path.read_text(encoding="utf-8")
            if "SystemMessage(" in source or "from langchain_core.messages import SystemMessage" in source:
                violations.append(str(path))

        # Assert
        assert violations == []
