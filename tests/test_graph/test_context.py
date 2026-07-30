"""AnalysisState 轻量上下文分组测试。"""

from __future__ import annotations

import ast
from pathlib import Path


class TestAnalysisStateContext:
    """覆盖功能 20.28、20.29 的上下文分组、兼容读取和节点收口。"""

    def test_build_context_groups_excludes_rich_execution_values(self) -> None:
        """上下文分组只能保存元数据，不得复制 SQL、结果、Schema 或图表。"""
        # Arrange
        from src.graph.context import build_context_groups

        state = {
            "user_query": "统计订单",
            "session_id": "session-1",
            "tenant_id": 3,
            "user_id": 8,
            "intent": "query",
            "datasource": "warehouse",
            "dialect": "postgres",
            "relevant_tables": [{"name": "orders", "columns": [{"name": "id"}]}],
            "generated_sql": "SELECT * FROM orders",
            "resolved_schema": object(),
            "query_result_sample": [{"id": 1}],
            "chart_config": {"type": "table"},
        }

        # Act
        groups = build_context_groups(state)

        # Assert
        assert groups["request_context"]["session_id"] == "session-1"
        assert groups["routing_context"]["datasource"] == "warehouse"
        assert groups["execution_context"]["table_names"] == ["orders"]
        serialized_keys = set().union(*(group.keys() for group in groups.values()))
        assert "generated_sql" not in serialized_keys
        assert "resolved_schema" not in serialized_keys
        assert "query_result_sample" not in serialized_keys
        assert "chart_config" not in serialized_keys

    def test_grouped_context_remains_compatible_without_flat_fields(self) -> None:
        """迁移完成后的消费者应能从分组字段恢复请求和路由上下文。"""
        # Arrange
        from src.graph.context import build_request_context, build_routing_context

        state = {
            "request_context": {
                "user_query": "查询库存",
                "session_id": "session-2",
                "tenant_id": 4,
                "user_id": 9,
                "user_role": "analyst",
                "request_rate_limit_checked": True,
            },
            "routing_context": {
                "intent": "query",
                "task_plan": {"capability": "sql_analysis"},
                "datasource": "inventory",
                "selected_datasources": ["inventory"],
                "skill_activation_stage": "schema",
                "skill_candidate_ids": ["system:quality"],
                "activated_skill_ids": ["system:quality"],
            },
        }

        # Act
        request = build_request_context(state)
        routing = build_routing_context(state)

        # Assert
        assert request.user_query == "查询库存"
        assert request.request_rate_limit_checked is True
        assert routing.datasource == "inventory"
        assert routing.skill_activation_stage == "schema"

    async def test_prepare_turn_rebuilds_and_clears_execution_context(self) -> None:
        """新一轮必须保留当前请求身份，并清空上一轮执行摘要。"""
        # Arrange
        from src.graph.nodes.prepare_turn import prepare_turn_node

        state = {
            "user_query": "查询新一轮订单",
            "session_id": "session-3",
            "tenant_id": 0,
            "user_id": 0,
            "execution_context": {
                "dialect": "mysql",
                "table_names": ["old_orders"],
                "retry_count": 3,
                "execution_retry_count": 2,
                "sql_valid": True,
                "sql_explain_checked": True,
                "validation_error_count": 0,
                "explain_error_count": 0,
                "execution_error_type": "",
                "row_count": 99,
                "truncated": True,
            },
        }

        # Act
        result = await prepare_turn_node(state)

        # Assert
        assert result["request_context"]["user_query"] == "查询新一轮订单"
        assert result["execution_context"]["row_count"] == 0
        assert result["execution_context"]["table_names"] == []
        assert result["execution_context"]["retry_count"] == 0
        assert result["routing_context"]["intent"] == ""

    def test_workflow_routes_from_grouped_context_only(self) -> None:
        """扁平字段移除后，工作流仍应按分组路由并遵守重试计数。"""
        # Arrange
        from src.graph.workflow import after_layer4, route_by_intent

        state = {
            "routing_context": {
                "intent": "query",
                "task_plan": {"capability": "sql_analysis"},
                "datasource": "warehouse",
                "selected_datasources": ["warehouse", "archive"],
                "skill_activation_stage": "schema",
                "skill_candidate_ids": [],
                "activated_skill_ids": [],
            },
            "execution_context": {
                "dialect": "postgres",
                "table_names": ["orders"],
                "retry_count": 3,
                "execution_retry_count": 0,
                "sql_valid": False,
                "sql_explain_checked": False,
                "validation_error_count": 0,
                "explain_error_count": 1,
                "execution_error_type": "",
                "row_count": 0,
                "truncated": False,
            },
            "explain_errors": [{"type": "database_error"}],
        }

        # Act
        intent_target = route_by_intent(state)
        explain_target = after_layer4(state)

        # Assert
        assert intent_target == "multi_source_dispatch"
        assert explain_target == "build_response"

    def test_graph_consumers_do_not_read_flat_context_fields(self) -> None:
        """图节点不得绕过 read_contexts 重新读取四组上下文的扁平字段。"""
        # Arrange
        context_fields = {
            "user_query", "session_id", "tenant_id", "user_id", "user_role",
            "request_rate_limit_checked", "datasource_access", "allowed_columns",
            "row_filter_sql", "enabled_skill_ids", "intent", "task_plan",
            "datasource", "selected_datasources", "skill_activation_stage",
            "skill_candidate_ids", "activated_skill_ids", "dialect", "retry_count",
            "execution_retry_count", "sql_valid", "sql_explain_checked",
            "execution_error_type",
        }
        source_paths = [
            *Path("src/graph/nodes").glob("*.py"),
            *Path("src/graph/subgraphs").glob("*.py"),
            Path("src/graph/workflow.py"),
            Path("src/graph/skill_activation.py"),
        ]
        violations: list[str] = []

        # Act
        for path in source_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = node.func
                key = node.args[0]
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr == "get"
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "state"
                    and isinstance(key, ast.Constant)
                    and key.value in context_fields
                ):
                    violations.append(f"{path}:{node.lineno}:{key.value}")

        # Assert
        assert violations == []
