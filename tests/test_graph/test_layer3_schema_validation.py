"""layer3 两阶段 Schema 校验回归测试（spec/39 §6）。"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TestLayer3TwoStageValidation:
    """覆盖两阶段取数与阶段二短路条件。"""

    # 方法作用：验证无数据源时阶段二被短路，不尝试连库。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_stage_two_skipped_without_datasource(self, monkeypatch) -> None:
        """最小 state（无数据源）必须快速返回，不调用 SchemaManager。"""
        logger.debug("test_stage_two_skipped_without_datasource 入口")
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：若阶段二未被短路，下面的替身会让用例失败
        def _boom(*args, **kwargs):
            raise AssertionError("阶段二不应在无数据源时被调用")

        monkeypatch.setattr(
            "src.knowledge.schema_manager.get_schema_manager", _boom, raising=False
        )
        state = {
            "generated_sql": 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            "relevant_tables": [
                {"name": "finance_invoices", "columns": [{"name": "year_month"}]}
            ],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert
        assert result["sql_valid"] is False
        assert result["validation_errors"][0]["type"] == "column_reference"
        assert "created_at" in result["validation_errors"][0]["message"]
        logger.info("test_stage_two_skipped_without_datasource 完成")

    # 方法作用：验证拦截结果携带结构化 issues 供回喂重试使用。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_validation_errors_carry_structured_issues(self) -> None:
        """issues 必须含 table / available_columns，否则无法生成有效回喂。"""
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange
        state = {
            "generated_sql": 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"',
            "relevant_tables": [
                {
                    "name": "finance_invoices",
                    "columns": [{"name": "event_date"}, {"name": "year_month"}],
                }
            ],
        }

        # Act
        result = asyncio.run(layer3_validate_node(state))

        # Assert
        issues = result["validation_errors"][0]["issues"]
        assert issues[0]["table"] == "finance_invoices"
        assert "event_date" in issues[0]["available_columns"]
