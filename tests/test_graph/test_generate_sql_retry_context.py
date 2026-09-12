"""重试上下文回喂契约测试（spec/39 §8）。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TestRetryContextSchemaHallucination:
    """覆盖 schema 幻觉不回注失败 SQL 的契约。"""

    # 方法作用：验证列幻觉分支不回注失败 SQL，且给出可用列清单。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_column_reference_does_not_echo_failed_sql(self) -> None:
        """失败的 SQL 不得被回注，否则会强化幻觉。"""
        logger.debug("test_column_reference_does_not_echo_failed_sql 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        failed_sql = 'SELECT "i"."created_at" FROM "finance_invoices" AS "i"'
        state = {
            "generated_sql": failed_sql,
            "validation_errors": [
                {
                    "type": "column_reference",
                    "message": "列 'i.created_at' 不属于表 finance_invoices",
                    "issues": [
                        {
                            "type": "cross_table_column",
                            "datasource": "pg_main",
                            "table": "finance_invoices",
                            "column": "created_at",
                            "qualifier": "i",
                            "provided_by": ["orders"],
                            "available_columns": ["event_date", "year_month"],
                        }
                    ],
                }
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "finance_invoices"}])

        # Assert
        assert failed_sql not in context
        assert "event_date" in context
        assert "year_month" in context
        logger.info("test_column_reference_does_not_echo_failed_sql 完成")

    # 方法作用：验证无 issues 结构时退回原有行为，不破坏既有调用方。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_legacy_column_reference_without_issues_falls_back(self) -> None:
        """旧格式 validation_errors 不崩溃。"""
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": "SELECT amount FROM orders",
            "validation_errors": [
                {"type": "column_reference", "message": "列名校验失败: 列 'x' 不在可用列中"}
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "orders"}])

        # Assert
        assert isinstance(context, str)
        assert context

    # 方法作用：验证 schema 分支的 issue 条数被限制在 3 条并显式提示剩余数量。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_issue_list_is_bounded_with_truncation_notice(self) -> None:
        """高置信 issue 很多时只渲染前 3 条，截断必须给出提示而非静默丢弃。"""
        logger.debug("test_issue_list_is_bounded_with_truncation_notice 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        issues = [
            {
                "type": "unknown_column",
                "datasource": "pg_main",
                "table": f"table_{index}",
                "column": f"bad_col_{index}",
                "qualifier": "",
                "provided_by": [],
                "available_columns": [f"good_col_{index}"],
            }
            for index in range(1, 6)
        ]
        failed_sql = "SELECT bad_col_1 FROM table_1"
        state = {
            "generated_sql": failed_sql,
            "validation_errors": [
                {"type": "column_reference", "message": "列名校验失败", "issues": issues}
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "table_1"}])

        # Assert
        assert failed_sql not in context
        assert "good_col_1" in context
        assert "good_col_3" in context
        assert "good_col_4" not in context
        assert "table_4" not in context
        assert "另有 2 个字段引用错误未列出" in context
        logger.info("test_issue_list_is_bounded_with_truncation_notice 完成")

    # 方法作用：验证单表可用列超过上限时只列出前 20 个并提示总列数。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_available_columns_are_bounded_with_truncation_notice(self) -> None:
        """可用列很多时截断列表，但必须提示共多少列，避免模型误判列不存在。"""
        logger.debug("test_available_columns_are_bounded_with_truncation_notice 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        available = [f"col_{index:02d}" for index in range(1, 31)]
        state = {
            "generated_sql": "SELECT missing_col FROM wide_table",
            "validation_errors": [
                {
                    "type": "column_reference",
                    "message": "列名校验失败",
                    "issues": [
                        {
                            "type": "unknown_column",
                            "datasource": "pg_main",
                            "table": "wide_table",
                            "column": "missing_col",
                            "qualifier": "",
                            "provided_by": [],
                            "available_columns": available,
                        }
                    ],
                }
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "wide_table"}])

        # Assert
        assert "col_20" in context
        assert "col_21" not in context
        assert "（仅列出前 20 个，共 30 个）" in context
        logger.info("test_available_columns_are_bounded_with_truncation_notice 完成")

    # 方法作用：验证关键指令在 retry_error 段预算截断后仍落在前 1800 字符内。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_retry_instructions_survive_section_budget_truncation(self) -> None:
        """该段是硬上限 + 尾部截断，核心指令必须前置，否则超预算时被整行砍掉。"""
        logger.debug("test_retry_instructions_survive_section_budget_truncation 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange：3 条 issue、3 张表、每表 25 个长列名（超过每表 20 个上限），
        # 既必然超出 1800 字符预算，也会触发列清单截断提示
        issues = [
            {
                "type": "unknown_column",
                "datasource": "pg_main",
                "table": table,
                "column": f"legacy_amount_{index}",
                "qualifier": "",
                "provided_by": [],
                "available_columns": [
                    f"column_with_a_long_descriptive_name_{position:02d}"
                    for position in range(1, 26)
                ],
            }
            for index, table in enumerate(
                ("finance_invoices", "finance_orders", "finance_refunds"), start=1
            )
        ]
        state = {
            "generated_sql": "SELECT legacy_amount_1 FROM finance_invoices",
            "validation_errors": [
                {"type": "column_reference", "message": "列名校验失败", "issues": issues}
            ],
        }
        # _llm_generate 中 retry_error section 的硬上限
        section_max_chars = 1800

        # Act
        context = build_retry_context(state, 1, [{"name": "finance_invoices"}])
        context_last_attempt = build_retry_context(state, 2, [{"name": "finance_invoices"}])

        # Assert：超预算前提下，指令与第一批事实/截断提示仍必须在可见前缀内
        assert len(context) > section_max_chars
        budgeted = context[:section_max_chars]
        assert "禁止复用上一轮 SQL" in budgeted
        assert "finance_invoices" in budgeted
        assert "仅列出前" in budgeted or "未列出" in budgeted
        assert "最后一次尝试" in context_last_attempt[:section_max_chars]
        logger.info("test_retry_instructions_survive_section_budget_truncation 完成")

    # 方法作用：验证表名幻觉分支输出逐字节不变。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_hallucination_branch_output_is_unchanged(self) -> None:
        """新增 schema 分支不得改变既有 hallucination 文案。"""
        logger.debug("test_hallucination_branch_output_is_unchanged 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": "SELECT * FROM invented_table",
            "validation_errors": [
                {"type": "hallucination", "unknown_tables": ["invented_table"]}
            ],
        }
        expected = (
            "## 第 1 次生成：纠正表名幻觉\n"
            "上一轮引用了不存在的表：invented_table。\n"
            "合法物理表白名单（只能使用以下物理表或 SQL 内定义的 CTE）：\n"
            "- orders\n"
            "禁止复用上一轮 SQL，必须仅基于合法表名和字段 Schema 重新生成。"
        )

        # Act
        context = build_retry_context(state, 1, [{"name": "orders"}])

        # Assert
        assert context == expected
        logger.info("test_hallucination_branch_output_is_unchanged 完成")

    # 方法作用：验证同时存在表名幻觉与列引用错误时仍优先走 hallucination 分支。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_hallucination_takes_precedence_over_schema_issues(self) -> None:
        """分支顺序不得改变：表名幻觉优先于字段引用错误。"""
        logger.debug("test_hallucination_takes_precedence_over_schema_issues 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": "SELECT missing_col FROM invented_table",
            "validation_errors": [
                {"type": "hallucination", "unknown_tables": ["invented_table"]},
                {
                    "type": "column_reference",
                    "message": "列名校验失败",
                    "issues": [
                        {
                            "type": "unknown_column",
                            "datasource": "pg_main",
                            "table": "orders",
                            "column": "missing_col",
                            "qualifier": "",
                            "provided_by": [],
                            "available_columns": ["amount"],
                        }
                    ],
                },
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "orders"}])

        # Assert
        assert "纠正表名幻觉" in context
        assert "纠正字段引用错误" not in context
        logger.info("test_hallucination_takes_precedence_over_schema_issues 完成")
