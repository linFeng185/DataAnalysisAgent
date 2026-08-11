"""指标目录存储形态识别与确定性 SQL 回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    # 方法作用：验证 Schema 型指标目录会经过模型复检并输出结构化主题。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_catalog_direct_answer_uses_allowlisted_llm_review(self, monkeypatch) -> None:
        """模型可复检候选，但未知字段和明确非指标字段不得进入结果。"""
        # Arrange
        import src.llm.client as llm_client
        import src.llm.invocation as invocation_module
        from src.graph.nodes.llm_answer import llm_direct_answer_node
        from src.llm.output_contracts import IndicatorCatalogReviewOutput

        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: True)

        async def fake_review(*args, **kwargs):
            """返回包含真实指标、非指标和虚构字段的复检样本。"""
            return IndicatorCatalogReviewOutput.model_validate({"indicators": [
                {
                    "table": "daily_sales",
                    "column": "sales_amount",
                    "include": True,
                    "display_name": "销售额",
                    "theme": "销售与订单",
                },
                {
                    "table": "daily_sales",
                    "column": "f_sort",
                    "include": False,
                    "display_name": "排序",
                    "theme": "其他",
                },
                {
                    "table": "daily_sales",
                    "column": "invented_metric",
                    "include": True,
                    "display_name": "虚构指标",
                    "theme": "其他",
                },
            ]})

        monkeypatch.setattr(invocation_module, "invoke_structured", fake_review)
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
                        {"name": "f_sort", "type": "int", "comment": "排序序号"},
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
        groups = response["analysis"]["indicator_groups"]
        assert groups == [{
            "theme": "销售与订单",
            "indicators": [{
                "name": "销售额",
                "description": "销售额",
                "table": "daily_sales",
                "column": "sales_amount",
            }],
        }]
        assert "1 项业务指标" in response["analysis"]["summary"]
        assert response["needs_time_range"] is False

    # 方法作用：验证字段前缀和注释规则能过滤排序、主键与地理坐标。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_metric_numeric_columns_are_excluded_from_fallback_catalog(self) -> None:
        """模型不可用时，确定性目录也只能保留真实业务度量。"""
        # Arrange
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_display_groups,
        )

        tables = [{
            "name": "big_screen_month_area_sale",
            "columns": [
                {"name": "f_id", "type": "bigint", "comment": "主键ID"},
                {"name": "f_sort", "type": "int", "comment": "排序序号"},
                {"name": "f_latitude", "type": "decimal", "comment": "纬度"},
                {"name": "f_longitude", "type": "decimal", "comment": "经度"},
                {"name": "f_sale", "type": "decimal", "comment": "销售量"},
            ],
        }]

        # Act
        plan = build_indicator_catalog_plan(tables)
        groups = build_indicator_display_groups(plan)

        # Assert
        assert plan.tables[0].numeric_columns == [
            "f_id", "f_sort", "f_latitude", "f_longitude", "f_sale",
        ]
        assert plan.tables[0].metric_columns == ["f_sale"]
        assert [item.column for group in groups for item in group.indicators] == ["f_sale"]
        assert groups[0].theme == "销售与订单"

    # 方法作用：验证 Schema 型指标摘要直接列出主题和指标名称。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_schema_catalog_summary_lists_grouped_indicator_names(self) -> None:
        """指标目录正文不能只返回数量，必须回答具体有哪些指标。"""
        # Arrange
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_catalog_summary,
            build_indicator_display_groups,
        )

        tables = [
            {
                "name": "daily_sales",
                "columns": [
                    {"name": "stat_date", "type": "date", "comment": "统计日期"},
                    {"name": "sales_amount", "type": "decimal", "comment": "销售额"},
                    {"name": "order_count", "type": "bigint", "comment": "订单数"},
                ],
            },
            {
                "name": "device_operation",
                "columns": [
                    {"name": "f_date", "type": "date", "comment": "日期"},
                    {"name": "alarm_count", "type": "int", "comment": "报警设备数"},
                ],
            },
        ]
        plan = build_indicator_catalog_plan(tables)
        groups = build_indicator_display_groups(plan)

        # Act
        summary = build_indicator_catalog_summary(plan, groups=groups)

        # Assert
        assert "当前数据源确认 3 项业务指标" in summary
        assert "- 销售与订单（2 项）：销售额、订单数" in summary
        assert "- 设备运维（1 项）：报警设备数" in summary

    # 方法作用：验证指标目录显式隐藏无关长期记忆并贯穿统一响应。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_indicator_catalog_response_does_not_restore_irrelevant_knowledge(
        self,
        monkeypatch,
    ) -> None:
        """指标目录响应不得再次展示上一轮问题和 SQL 模板。"""
        # Arrange
        import src.graph.nodes.llm_answer as answer_module
        import src.memory.history_store as history_module
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_display_groups,
        )
        from src.graph.nodes.build_response import build_response_node

        tables = [{
            "name": "daily_orders",
            "columns": [
                {"name": "f_date", "type": "date", "comment": "日期"},
                {"name": "demand_orders", "type": "bigint", "comment": "需求工单数"},
            ],
        }]
        plan = build_indicator_catalog_plan(tables)
        groups = build_indicator_display_groups(plan)
        monkeypatch.setattr(
            history_module,
            "get_history_store",
            lambda: SimpleNamespace(add=AsyncMock()),
        )
        state = {
            "user_query": "东亚汽车里有哪些数据指标",
            "intent": "metadata",
            "task_plan": {"operation": "indicator_catalog"},
            "datasource": "dongya",
            "relevant_tables": tables,
            "conversation_history": [],
            "long_term_memories_text": (
                "问题: 分析一下所有的单日需求工单数。"
                " 方言: mysql SQL: SELECT f_date"
            ),
        }

        async def fixed_review(_plan):
            return groups

        import src.graph.indicator_catalog as catalog_module

        monkeypatch.setattr(catalog_module, "review_indicator_catalog", fixed_review)

        # Act
        direct_update = await answer_module.llm_direct_answer_node(state)
        response = (
            await build_response_node({**state, **direct_update})
        )["final_response"]

        # Assert
        assert response["source"] == "indicator_catalog"
        assert response["activated_knowledge"] == ""
        assert "需求工单数" in response["analysis"]["summary"]
        assert "SQL: SELECT" not in response["analysis"]["summary"]

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
