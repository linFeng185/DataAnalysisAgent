"""重试上下文回喂契约测试（spec/39 §8）。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

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

    # 方法作用：验证单表可用列超过上限时只列出前 10 个并提示总列数。
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

        # Assert：截断提示前置在行首、与表名同行，避免它自己被尾部截断砍掉
        assert "col_10" in context
        assert "col_11" not in context
        assert "实际可用列（仅列出前 10 个，共 30 个）：" in context
        logger.info("test_available_columns_are_bounded_with_truncation_notice 完成")

    # 方法作用：验证列清单被校验器截断时「共 N 个」报真实总数而非截断后的条数。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_truncated_column_list_reports_true_total(self) -> None:
        """B4 回归：60 列表在 issue 里只带 40 条，文案必须说「共 60 个」。"""
        logger.debug("test_truncated_column_list_reports_true_total 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange：available_columns 是校验器按 MAX_AVAILABLE_COLUMNS=40 截断后的视图
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
                            "available_columns": [
                                f"col_{index:02d}" for index in range(1, 41)
                            ],
                            "available_columns_total": 60,
                        }
                    ],
                }
            ],
        }

        # Act
        context = build_retry_context(state, 1, [{"name": "wide_table"}])

        # Assert：真实总数必须进文案，否则模型把 40 当全部、剩余 20 列被当成不存在
        assert "实际可用列（仅列出前 10 个，共 60 个）：" in context
        assert "共 40 个" not in context
        logger.info("test_truncated_column_list_reports_true_total 完成")

    # 方法作用：验证关键指令在 retry_error 段预算截断后仍落在前 1800 字符内。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_retry_instructions_survive_section_budget_truncation(self) -> None:
        """该段是硬上限 + 尾部截断，核心指令必须前置，否则超预算时被整行砍掉。"""
        logger.debug("test_retry_instructions_survive_section_budget_truncation 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange：极端场景——3 条 cross_table_column issue（每条 8 个长提供方表名）
        # 把「表行之前」的明细撑到约 1300 字符，使首条表行起点约 1416；
        # 每表 25 个 58 字符长列名（超过每表 10 个上限），首条表行约 455 字符。
        # 实测总长 3362 > 1800：截断提示若挂在行尾（约 1871）会被尾部砍掉，
        # 前置到行首（约 1416）则仍在可见前缀内——正是本用例要钉住的差异。
        issues = [
            {
                "type": "cross_table_column",
                "datasource": "pg_main",
                "table": table,
                "column": f"legacy_amount_{index}",
                "qualifier": "t" * 12,
                "provided_by": [
                    f"cross_source_provider_table_with_long_name_{position:03d}"
                    for position in range(8)
                ],
                "available_columns": [
                    f"column_with_an_extremely_long_descriptive_business_name_{position:02d}"
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

        # Assert：超预算前提下，指令、第一批事实与「截断提示」都必须在可见前缀内
        assert len(context) > section_max_chars
        budgeted = context[:section_max_chars]
        assert "禁止复用上一轮 SQL" in budgeted
        assert "finance_invoices" in budgeted
        # 截断提示若挂在清单末尾会与清单一起被尾部截断，模型将拿到无标记的
        # 残缺列清单并误判列不存在——这里钉住它必须前置可见。
        assert "仅列出前" in budgeted
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

    # 方法作用：验证歧义列（列真实存在于多张候选表）不被渲染成「不存在的字段」。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_ambiguous_column_renders_qualifier_guidance(self, monkeypatch) -> None:
        """state 由真实 layer3 节点产出：歧义列必须要求补表限定符并给出候选表。

        A1 回归：修复前这里渲染成「上一轮引用了不存在的字段：pg_main.customers, orders.id
        （该列属于: 未知）」，把真实存在的列谎报为不存在，模型会去改列名而非加限定符。
        """
        logger.debug("test_ambiguous_column_renders_qualifier_guidance 入口")
        import asyncio

        from src.graph.nodes.generate_sql import build_retry_context
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：两张表都有 id，SQL 未加限定符引用 id → 真实校验路径判为歧义（high）
        state, tables = _arrange_ambiguous_column_case(monkeypatch)

        # Act：先跑真实 layer3 节点产出 validation_errors，再进重试上下文渲染
        result = asyncio.run(layer3_validate_node(state))
        context = build_retry_context(
            {**state, "validation_errors": result["validation_errors"]}, 1, tables
        )

        # Assert：拦截成立，且文案不得否定该列的存在，必须给出限定符指引与候选表
        issues = result["validation_errors"][0]["issues"]
        assert any(item["type"] == "ambiguous_column" for item in issues)
        assert "不存在" not in context
        assert "表限定符" in context
        assert "customers" in context
        assert "orders" in context
        logger.info("test_ambiguous_column_renders_qualifier_guidance 完成")

    # 方法作用：验证 retry=2 的歧义列场景不出现指向不存在列清单的悬挂引用。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_ambiguous_column_at_last_attempt_has_no_dangling_list_reference(
        self, monkeypatch
    ) -> None:
        """retry=2 时下方没有可用列清单，最后通牒行不得说「使用下列明确列出的标识符」。

        回归：修复前最后通牒行不按 has_column_lists 分派，歧义列场景会指向一段
        并不存在的列清单（与 A1 修复的首指令行悬挂引用同类）。
        """
        logger.debug(
            "test_ambiguous_column_at_last_attempt_has_no_dangling_list_reference 入口"
        )
        import asyncio

        from src.graph.nodes.generate_sql import build_retry_context
        from src.graph.nodes.layer3_validate import layer3_validate_node

        # Arrange：真实 layer3 产出歧义列 issue（available_columns 为空）
        state, tables = _arrange_ambiguous_column_case(monkeypatch)

        # Act：retry=2（最后一次尝试）渲染重试上下文
        result = asyncio.run(layer3_validate_node(state))
        context = build_retry_context(
            {**state, "validation_errors": result["validation_errors"]}, 2, tables
        )

        # Assert：最后通牒必须存在，但不得引用未渲染的列清单；限定符指引仍在
        assert "最后一次尝试" in context
        assert "必须使用下列明确列出的标识符" not in context
        assert "实际可用列" not in context
        assert "这是最后一次尝试，必须严格按上述问题逐条修正后重新生成。" in context
        assert "表限定符" in context
        logger.info(
            "test_ambiguous_column_at_last_attempt_has_no_dangling_list_reference 完成"
        )


class TestRetryContextExplainErrors:
    """覆盖 EXPLAIN 校验失败必须回注真实错误原因的契约（spec/39 §8）。

    回归背景：build_retry_context 只读 validation_errors 与 execution_error，
    layer4_explain 写入的 explain_errors 从未进入重试 Prompt。现象是重试段渲染成
    「错误原因: []」，模型拿不到 DB 报错文本，三次重试产出逐字节相同的 SQL
    （实测 ClickHouse code 184 与 Oracle ORA-00942 两条链路均命中）。
    """

    # 方法作用：验证 EXPLAIN 失败时数据库错误文本被回注。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_explain_error_reason_is_fed_back(self) -> None:
        """只有 explain_errors 时，错误原因不得再渲染成空列表。"""
        logger.debug("test_explain_error_reason_is_fed_back 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange：ClickHouse 真实故障报文
        state = {
            "generated_sql": (
                'SELECT round(sum(attributed_revenue), 2) AS attributed_revenue, '
                "uniqExact(order_id) AS attributed_order_count "
                "FROM insight_order_attribution"
            ),
            "validation_errors": [],
            "execution_error": "",
            "explain_errors": [
                {
                    "type": "semantic_error",
                    "message": (
                        "Code: 184. DB::Exception: Aggregate function "
                        "sum(attributed_revenue) is found inside another aggregate "
                        "function in query. (ILLEGAL_AGGREGATION)"
                    ),
                }
            ],
        }

        # Act
        context = build_retry_context(state, 1, [])

        # Assert：模型必须看到可据以改写的报错原文
        assert "错误原因: []" not in context
        assert "ILLEGAL_AGGREGATION" in context
        assert "is found inside another aggregate" in context
        logger.info("test_explain_error_reason_is_fed_back 完成")

    # 方法作用：验证错误原因排在失败 SQL 之前，避免被尾部截断先砍掉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_explain_error_reason_precedes_failed_sql(self) -> None:
        """retry_error section 是尾部截断，「原因」必须在「失败 SQL」之前。"""
        logger.debug("test_explain_error_reason_precedes_failed_sql 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": 'SELECT SYSDATE FROM "dual"',
            "validation_errors": [],
            "execution_error": "",
            "explain_errors": [
                {
                    "type": "semantic_error",
                    "message": "ORA-00942: table or view does not exist",
                }
            ],
        }

        # Act
        context = build_retry_context(state, 1, [])

        # Assert
        assert context.index("ORA-00942") < context.index("失败的 SQL")
        logger.info("test_explain_error_reason_precedes_failed_sql 完成")

    # 方法作用：验证超预算截断后错误原因与核心指令仍在可见前缀内。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_explain_reason_survives_section_budget_truncation(self) -> None:
        """数千字符失败 SQL + 500 字符报错，1800 段预算内原因必须可见。"""
        logger.debug("test_explain_reason_survives_section_budget_truncation 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        long_sql = "SELECT " + ", ".join(f"col_{index}" for index in range(400))
        state = {
            "generated_sql": long_sql,
            "validation_errors": [],
            "execution_error": "",
            "explain_errors": [
                {
                    "type": "semantic_error",
                    "message": "ORA-00942: table or view does not exist" + "x" * 464,
                }
            ],
        }
        section_max_chars = 1800

        # Act
        context = build_retry_context(state, 1, [])

        # Assert
        budgeted = context[:section_max_chars]
        assert "ORA-00942" in budgeted
        assert "禁止逐字复用上一轮 SQL" in budgeted
        logger.info("test_explain_reason_survives_section_budget_truncation 完成")

    # 方法作用：验证 EXPLAIN 错误条数受限且披露未列出的数量。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_explain_errors_are_bounded_with_notice(self) -> None:
        """错误很多时只渲染前 3 条，且必须显式提示剩余数量。"""
        logger.debug("test_explain_errors_are_bounded_with_notice 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": "SELECT 1 FROM orders",
            "validation_errors": [],
            "execution_error": "",
            "explain_errors": [
                {"type": "semantic_error", "message": f"错误-{index}"}
                for index in range(5)
            ],
        }

        # Act
        context = build_retry_context(state, 1, [])

        # Assert
        assert "错误-0" in context
        assert "错误-2" in context
        assert "错误-3" not in context
        assert "另有 2 条 EXPLAIN 错误未列出" in context
        logger.info("test_explain_errors_are_bounded_with_notice 完成")

    # 方法作用：验证超长单条报错被截断并留下可见标记。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_long_explain_message_is_truncated_with_marker(self) -> None:
        """单条 message 过长必须截断，避免一条报错吃掉整个重试段预算。"""
        logger.debug("test_long_explain_message_is_truncated_with_marker 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange：连续 900 个 y，任何 500 长度的窗口都能测出是否被截
        state = {
            "generated_sql": "SELECT 1 FROM orders",
            "validation_errors": [],
            "execution_error": "",
            "explain_errors": [
                {"type": "semantic_error", "message": "ORA-00942" + "y" * 900}
            ],
        }

        # Act
        context = build_retry_context(state, 1, [])

        # Assert
        assert "ORA-00942" in context
        assert "y" * 500 not in context
        assert "（已截断）" in context
        logger.info("test_long_explain_message_is_truncated_with_marker 完成")

    # 方法作用：验证 EXPLAIN 错误与旧字段错误同时存在时都会回注。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_explain_errors_merge_with_validation_errors(self) -> None:
        """通用分支必须同时带上 validation_errors 与 explain_errors。"""
        logger.debug("test_explain_errors_merge_with_validation_errors 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": "SELECT amount FROM orders",
            "validation_errors": [
                {"type": "sql_semantic", "message": "校验器报出的语义问题"}
            ],
            "execution_error": "",
            "explain_errors": [
                {"type": "semantic_error", "message": "EXPLAIN 报出的数据库错误"}
            ],
        }

        # Act
        context = build_retry_context(state, 1, [])

        # Assert
        assert "校验器报出的语义问题" in context
        assert "EXPLAIN 报出的数据库错误" in context
        logger.info("test_explain_errors_merge_with_validation_errors 完成")

    # 方法作用：验证无任何错误证据时给出可操作的兜底文案而非空列表。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_empty_error_evidence_gives_actionable_fallback(self) -> None:
        """结构化错误全空时不得渲染裸 `[]`，否则模型误以为没有错误。"""
        logger.debug("test_empty_error_evidence_gives_actionable_fallback 入口")
        from src.graph.nodes.generate_sql import build_retry_context

        # Arrange
        state = {
            "generated_sql": "SELECT 1 FROM orders",
            "validation_errors": [],
            "execution_error": "",
            "explain_errors": [],
        }

        # Act
        context = build_retry_context(state, 1, [])

        # Assert
        assert "错误原因: []" not in context
        assert "未记录结构化错误" in context
        logger.info("test_empty_error_evidence_gives_actionable_fallback 完成")


class TestExplainErrorsReachRetryPrompt:
    """端到端接线：layer4_explain 拒绝后，下一轮 generate_sql 必须拿到 DB 报错原文。

    单测 build_retry_context 只能证明「给了 explain_errors 会渲染」，
    证明不了「layer4 写的 explain_errors 真的会流到 generate_sql 的入参里」。
    这里跑真实 layer4_explain_node 与真实 generate_sql_node，只把最外层的
    EXPLAIN 委托与 LLM 调用换成桩，钉住两个节点之间的接线。
    """

    # 方法作用：验证 layer4 写出的 explain_errors 经真实节点链路进入重试 Prompt。
    # Args: self - pytest 用例实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败由 pytest 报告。
    async def test_layer4_explain_errors_reach_generate_sql_retry_prompt(
        self, monkeypatch
    ) -> None:
        """EXPLAIN 拒绝 → _llm_generate 收到的 error_ctx 必须含 DB 原文。"""
        logger.debug("test_layer4_explain_errors_reach_generate_sql_retry_prompt 入口")
        import src.graph.nodes.generate_sql as generate_module
        import src.security.sql_execution as sql_execution
        from src.graph.nodes.layer4_explain import layer4_explain_node

        # Arrange：ClickHouse 真实故障报文，SQL 用真实触发别名的写法
        db_error = (
            "Code: 184. DB::Exception: Aggregate function sum(attributed_revenue) "
            "is found inside another aggregate function in query. (ILLEGAL_AGGREGATION)"
        )
        failed_sql = (
            "SELECT round(sum(attributed_revenue), 2) AS attributed_revenue, "
            "uniqExact(order_id) AS attributed_order_count "
            "FROM insight_order_attribution"
        )

        # 只替换最外层 EXPLAIN 委托，layer4 的错误写回逻辑保持真实
        async def fake_explain(sql, datasource, dialect, *, tenant_id=None):
            logger.debug("EXPLAIN 委托桩命中", datasource=datasource, dialect=dialect)
            return sql_execution.SQLExecutionResult(
                success=False,
                sql=sql,
                dialect=dialect,
                error=db_error,
                error_type="sql_semantic",
                details=[{"type": "semantic_error", "message": db_error}],
            )

        monkeypatch.setattr(sql_execution, "validate_and_explain_sql", fake_explain)
        state = {
            "user_query": "近 90 天归因收入",
            "datasource": "ch",
            "dialect": "clickhouse",
            "generated_sql": failed_sql,
            "relevant_tables": [{"name": "insight_order_attribution", "columns": []}],
            "validation_errors": [],
            "execution_error": "",
        }
        # layer4 依赖分组上下文（真实 read_contexts 要求完整分组 state），
        # 仅在该节点调用期间替换，不干扰后面 generate_sql 的真实上下文解析
        contexts = SimpleNamespace(
            routing=SimpleNamespace(datasource="ch", intent="query", task_plan={}),
            execution=SimpleNamespace(dialect="clickhouse", retry_count=0),
            request=SimpleNamespace(user_query="近 90 天归因收入"),
        )
        with monkeypatch.context() as scope:
            scope.setattr(
                "src.graph.context.read_contexts", lambda _state: contexts, raising=False
            )
            layer4_update = await layer4_explain_node(state)

        # Act：把 layer4 的真实产出喂给真实 generate_sql（retry_count=1 触发重试段）
        captured: dict[str, object] = {}

        async def fake_llm(**kwargs):
            captured.update(kwargs)
            return "SELECT 1", "", "ok"

        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", fake_llm)
        await generate_module.generate_sql_node(
            {**state, **layer4_update, "retry_count": 1}, {}
        )

        # Assert：接线两端都要成立
        assert layer4_update["sql_valid"] is False
        assert layer4_update["explain_errors"]
        error_ctx = str(captured["error_ctx"])
        assert "ILLEGAL_AGGREGATION" in error_ctx
        assert "错误原因: []" not in error_ctx
        logger.info("test_layer4_explain_errors_reach_generate_sql_retry_prompt 完成")


# 方法作用：搭建「两张表都有 id、SQL 未加限定符引用 id」的真实 layer3 校验现场。
# Args: monkeypatch - pytest 补丁工具，用于注入上下文与 SchemaManager 桩。
# Returns: (state, tables) 元组：待校验 state 与合法表清单。
def _arrange_ambiguous_column_case(monkeypatch) -> tuple[dict, list[dict]]:
    """两张表都有 id 且 SQL 未限定引用 id → 真实校验路径判为歧义（high）。"""
    logger.debug("歧义列校验现场搭建入口")
    contexts = SimpleNamespace(
        routing=SimpleNamespace(datasource="pg_main"),
        execution=SimpleNamespace(dialect="postgres"),
    )
    monkeypatch.setattr(
        "src.graph.context.read_contexts", lambda state: contexts, raising=False
    )
    tables = [
        {"name": "customers", "columns": [{"name": "id"}, {"name": "name"}]},
        {"name": "orders", "columns": [{"name": "id"}, {"name": "amount"}]},
    ]
    # 权威快照与阶段一一致 → 阶段二复核确认歧义，不会被假阳性清除
    manager = _SnapshotStub(
        SimpleNamespace(
            tables=[
                SimpleNamespace(
                    name=table["name"],
                    columns=[
                        SimpleNamespace(name=column["name"])
                        for column in table["columns"]
                    ],
                )
                for table in tables
            ]
        )
    )
    monkeypatch.setattr(
        "src.knowledge.schema_manager.get_schema_manager",
        lambda: manager,
        raising=False,
    )
    state = {
        "generated_sql": "SELECT id FROM customers JOIN orders ON customers.id = orders.id",
        "relevant_tables": tables,
    }
    logger.info("歧义列校验现场搭建完成")
    return state, tables


# 方法作用：为歧义列回归用例提供阶段二取数桩，返回固定快照且不触碰真实连接。
# Args: snapshot - get_or_fetch_schema 的返回值。
# Returns: 具备异步 get_or_fetch_schema 的桩实例。
class _SnapshotStub:
    """极简 SchemaManager 桩：只实现节点用到的异步取数入口。"""

    # 方法作用：保存待返回的快照。
    # Args: snapshot - 取数成功时返回的快照桩。
    # Returns: 无返回值。
    def __init__(self, snapshot: SimpleNamespace) -> None:
        self._snapshot = snapshot
        logger.debug("歧义列快照桩构造完成")

    # 方法作用：模拟权威取数，始终返回构造时给定的快照。
    # Args: datasource - 数据源名；user_query - 语义筛选问题；tenant_id - 显式租户。
    # Returns: 固定快照桩。
    async def get_or_fetch_schema(
        self, datasource: str, user_query: str = "", *, tenant_id: int | None = None
    ) -> SimpleNamespace:
        logger.debug(
            f"歧义列快照桩取数 datasource={datasource} tenant_id={tenant_id}"
        )
        return self._snapshot
