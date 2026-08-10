"""指标目录存储形态识别与确定性 SQL 回归测试。"""

from __future__ import annotations

import pytest
import sqlglot


class TestIndicatorCatalog:
    """覆盖指标目录的表式、宽表字段式和行式存储边界。"""

    # 方法作用：验证宽表数值字段被识别为指标候选但不查询业务数据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_wide_table_uses_schema_catalog(self) -> None:
        """多个数值业务字段应通过 Schema 列目录回答。"""
        # Arrange
        from src.graph.indicator_catalog import build_indicator_catalog_plan

        tables = [
            {
                "name": "daily_sales",
                "description": "每日销售指标",
                "columns": [
                    {"name": "stat_date", "type": "date", "comment": "统计日期"},
                    {"name": "sales_amount", "type": "decimal", "comment": "销售额"},
                    {"name": "order_count", "type": "bigint", "comment": "订单数"},
                ],
            }
        ]

        # Act
        plan = build_indicator_catalog_plan(tables)

        # Assert
        assert plan.mode == "schema_catalog"
        assert plan.tables[0].storage_mode == "wide_columns"
        assert plan.tables[0].metric_columns == ["sales_amount", "order_count"]
        assert plan.requires_data_query is False

    # 方法作用：验证指标字段结构问法不被目录规则抢占。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_indicator_column_question_is_not_catalog(self) -> None:
        """“指标有哪些字段”应保留 schema_explanation 语义。"""
        # Arrange
        from src.graph.indicator_catalog import is_indicator_catalog_query

        # Act / Assert
        assert is_indicator_catalog_query("指标有哪些字段") is False

    # 方法作用：验证单值指标表按表或视图代表指标域处理。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_single_metric_table_uses_schema_only(self) -> None:
        """只有一个数值指标字段时仍只读取表名、字段和注释。"""
        # Arrange
        from src.graph.indicator_catalog import build_indicator_catalog_plan

        tables = [
            {
                "name": "daily_attendance",
                "description": "每日实到人数",
                "columns": [
                    {"name": "stat_date", "type": "date"},
                    {"name": "actual_count", "type": "int", "comment": "实到人数"},
                ],
            }
        ]

        # Act
        plan = build_indicator_catalog_plan(tables)

        # Assert
        assert plan.mode == "schema_catalog"
        assert plan.tables[0].storage_mode == "table_metric"
        assert plan.requires_data_query is False

    # 方法作用：验证行式指标表生成只枚举编码和名称的受限 DISTINCT SQL。
    # Args: self - pytest 测试类实例；dialect - SQL 输出方言；parser_dialect - sqlglot 校验方言。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("dialect", "parser_dialect"),
        [
            ("mysql", "mysql"),
            ("postgres", "postgres"),
            ("clickhouse", "clickhouse"),
        ],
    )
    def test_row_metric_table_builds_distinct_catalog_sql(
        self,
        dialect: str,
        parser_dialect: str,
    ) -> None:
        """metric_code/name/value 结构必须查询目录行，但不得扫描指标值。"""
        # Arrange
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_catalog_sql,
        )

        tables = [
            {
                "name": "metric_values",
                "columns": [
                    {"name": "metric_code", "type": "varchar"},
                    {"name": "metric_name", "type": "varchar"},
                    {"name": "metric_value", "type": "decimal"},
                    {"name": "stat_date", "type": "date"},
                ],
            }
        ]

        # Act
        plan = build_indicator_catalog_plan(tables)
        sql = build_indicator_catalog_sql(plan, dialect)

        # Assert
        assert plan.mode == "row_catalog"
        assert plan.requires_data_query is True
        expression = sqlglot.parse_one(sql, read=parser_dialect)
        referenced_columns = {column.name for column in expression.find_all(sqlglot.exp.Column)}
        assert "metric_value" not in referenced_columns
        assert "DISTINCT" in sql.upper()
        assert "LIMIT 1000" in sql.upper()
        assert expression is not None

    # 方法作用：验证多个行式候选表不会被系统擅自选择。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_multiple_row_metric_tables_require_target_table(self) -> None:
        """多个目录表存在时应询问业务域或目标表，不询问时间。"""
        # Arrange
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_catalog_sql,
            build_indicator_catalog_summary,
        )

        tables = [
            {
                "name": name,
                "columns": [
                    {"name": "metric_code", "type": "varchar"},
                    {"name": "metric_name", "type": "varchar"},
                    {"name": "metric_value", "type": "decimal"},
                ],
            }
            for name in ("sales_metrics", "production_metrics")
        ]

        # Act
        plan = build_indicator_catalog_plan(tables)
        summary = build_indicator_catalog_summary(plan)

        # Assert
        assert plan.mode == "ambiguous"
        assert plan.needs_input is True
        assert build_indicator_catalog_sql(plan, "postgres") == ""
        assert "目标表" in summary
        assert "时间范围" not in summary

    # 方法作用：验证空 Schema 返回明确补充请求而不是时间范围提示。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_empty_schema_requires_business_scope(self) -> None:
        """没有表结构证据时不得编造指标。"""
        # Arrange
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_catalog_summary,
        )

        # Act
        plan = build_indicator_catalog_plan([])
        summary = build_indicator_catalog_summary(plan)

        # Assert
        assert plan.mode == "unavailable"
        assert plan.needs_input is True
        assert "业务域" in summary
        assert "时间范围" not in summary

    # 方法作用：验证 Schema 型指标目录直接回答不会调用任何 LLM。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_catalog_direct_answer_bypasses_llm(self, monkeypatch) -> None:
        """表式和宽表字段式目录必须完全由真实 Schema 生成。"""
        # Arrange
        import src.llm.client as llm_client
        from src.graph.nodes.llm_answer import llm_direct_answer_node

        monkeypatch.setattr(
            llm_client,
            "is_task_llm_available",
            lambda task: (_ for _ in ()).throw(AssertionError(f"不应调用 LLM: {task}")),
        )
        state = {
            "user_query": "有哪些数据指标",
            "intent": "metadata",
            "task_plan": {"operation": "indicator_catalog"},
            "relevant_tables": [
                {
                    "name": "daily_sales",
                    "columns": [
                        {"name": "stat_date", "type": "date"},
                        {"name": "sales_amount", "type": "decimal", "comment": "销售额"},
                        {"name": "order_count", "type": "bigint", "comment": "订单数"},
                    ],
                }
            ],
        }

        # Act
        result = await llm_direct_answer_node(state)

        # Assert
        response = result["final_response"]
        assert response["source"] == "indicator_catalog"
        assert response["sql"] == ""
        assert "sales_amount（销售额）" in response["analysis"]["summary"]
        assert response["needs_time_range"] is False

    # 方法作用：验证行式指标目录节点使用确定性 SQL 且不调用 LLM。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_row_catalog_generate_node_bypasses_llm(self, monkeypatch) -> None:
        """唯一行式指标表只枚举编码和名称，不生成或执行指标值查询。"""
        # Arrange
        import src.graph.nodes.generate_sql as generate_module

        monkeypatch.setattr(
            generate_module,
            "is_llm_available",
            lambda: (_ for _ in ()).throw(AssertionError("不应调用 LLM")),
        )
        state = {
            "user_query": "有哪些数据指标",
            "intent": "metadata",
            "task_plan": {"operation": "indicator_catalog"},
            "dialect": "postgres",
            "relevant_tables": [
                {
                    "name": "metric_values",
                    "columns": [
                        {"name": "metric_code", "type": "varchar"},
                        {"name": "metric_name", "type": "varchar"},
                        {"name": "metric_value", "type": "numeric"},
                    ],
                }
            ],
        }

        # Act
        result = await generate_module.generate_sql_node(state, {})

        # Assert
        expression = sqlglot.parse_one(result["generated_sql"], read="postgres")
        referenced_columns = {column.name for column in expression.find_all(sqlglot.exp.Column)}
        assert referenced_columns == {"metric_code", "metric_name"}
        assert result["needs_time_range"] is False
