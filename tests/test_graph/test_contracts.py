"""任务计划与统一分析产物契约测试。"""

from __future__ import annotations

import pytest


class TestTaskPlan:
    """覆盖第一阶段任务路由契约。"""

    def test_build_task_plan_maps_trend_and_missing_time_range(self) -> None:
        """趋势请求缺少时间范围时必须显式记录缺失参数。"""
        # Arrange
        from src.graph.contracts import build_task_plan

        # Act
        plan = build_task_plan("trend", query="用电总量趋势分析", datasources=["dongya"])

        # Assert
        assert plan.capability == "sql_analysis"
        assert plan.operation == "trend"
        assert plan.datasources == ["dongya"]
        assert plan.needs_time_range is True
        assert plan.missing_inputs == ["time_range"]

    def test_build_task_plan_keeps_direct_and_file_capabilities(self) -> None:
        """直接回答和文件分析必须路由到独立能力，而不是 SQL。"""
        # Arrange
        from src.graph.contracts import build_task_plan

        # Act
        direct = build_task_plan("chat", query="你好")
        file_plan = build_task_plan("file_analysis", query="分析上传的 CSV")

        # Assert
        assert direct.capability == "direct_answer"
        assert file_plan.capability == "file_analysis"


class TestMultiSourceContracts:
    """覆盖功能 20.26 的多数据源请求与结果契约。"""

    def test_source_request_builds_minimal_worker_state(self) -> None:
        """单源请求只携带白名单字段，不得继承上一轮执行产物。"""
        # Arrange
        from src.graph.contracts import SourceQueryRequest

        state = {
            "user_query": "汇总两个库的订单数",
            "selected_datasources": ["mysql", "postgres"],
            "tenant_id": 2,
            "user_id": 7,
            "datasource_access": {
                "mysql": {"allowed_columns": ["orders.id"], "row_filter_sql": "tenant_id=2"},
            },
            "generated_sql": "SELECT * FROM stale_table",
            "execution_error": "上一轮失败",
            "query_result_sample": [{"stale": True}],
            "chart_config": {"type": "line"},
        }

        # Act
        request = SourceQueryRequest.from_state("mysql", state)
        worker_state = request.to_worker_state(resolved_schema=object(), dialect="mysql")

        # Assert
        assert request.permission.allowed_columns == ["orders.id"]
        assert worker_state["row_filter_sql"] == "tenant_id=2"
        assert "当前只负责数据源 `mysql`" in worker_state["user_query"]
        assert "generated_sql" not in worker_state
        assert "execution_error" not in worker_state
        assert "query_result_sample" not in worker_state
        assert "chart_config" not in worker_state
        assert worker_state["execution_context"]["dialect"] == "mysql"

    def test_multi_source_result_rejects_missing_source(self) -> None:
        """结果未覆盖全部已选来源时必须校验失败，不能静默部分成功。"""
        # Arrange
        from pydantic import ValidationError

        from src.graph.contracts import MultiSourceResult, SourceQueryResult

        result = SourceQueryResult(
            datasource="mysql",
            success=True,
            sql="SELECT 1",
        )

        # Act / Assert
        with pytest.raises(ValidationError, match="未覆盖全部已选来源"):
            MultiSourceResult.from_results(
                query="比较订单数",
                selected_datasources=["mysql", "postgres"],
                results=[result],
            )

    def test_multi_source_result_computes_outcome_counts(self) -> None:
        """多源批次必须根据来源明细计算成功和失败数量。"""
        # Arrange
        from src.graph.contracts import MultiSourceResult, SourceQueryResult

        results = [
            SourceQueryResult(datasource="mysql", success=True, sql="SELECT 1"),
            SourceQueryResult.failed("postgres", "连接失败"),
        ]

        # Act
        batch = MultiSourceResult.from_results(
            query="比较订单数",
            selected_datasources=["mysql", "postgres"],
            results=results,
        )

        # Assert
        assert batch.success_count == 1
        assert batch.failure_count == 1
        assert batch.to_legacy_results()[1] == {
            "datasource": "postgres",
            "success": False,
            "error": "连接失败",
        }


class TestAnalysisArtifactIntegration:
    """覆盖 build_response 对统一产物的兼容接入。"""

    async def test_build_response_contains_reproducible_artifact(self) -> None:
        """旧响应字段保留的同时，必须生成证据和复现信息。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        state = {
            "user_query": "统计订单数",
            "datasource": "warehouse",
            "session_id": "session-1",
            "generated_sql": "SELECT COUNT(*) AS total FROM orders",
            "query_result_sample": [{"total": 3}],
            "query_result_full_count": 1,
            "analysis_result": {"summary": "订单数为 3", "confidence": "high"},
            "chart_config": {"type": "table", "option": {}},
            "conversation_history": [],
        }

        # Act
        result = await build_response_node(state)

        # Assert
        artifact = result["final_response"]["artifact"]
        assert artifact["kind"] == "table"
        assert artifact["evidence"]
        assert artifact["reproducibility"]["sql_hash"]
        assert result["final_response"]["sql"] == state["generated_sql"]
