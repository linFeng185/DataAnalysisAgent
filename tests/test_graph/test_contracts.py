"""任务计划与统一分析产物契约测试。"""

from __future__ import annotations

import pytest


class TestTaskPlan:
    """覆盖第一阶段任务路由契约。"""

    # 验证真实英文月份和数值日期不会重复要求补充时间。
    # Args: self - 实例；expression - 日期表达；intent - 需要时间的分析类型。
    # Returns: 无返回值。
    @pytest.mark.parametrize("intent", ["trend", "aggregation", "attribution"])
    @pytest.mark.parametrize("expression", [
        "from January 1 to January 7, 2026",
        "from Jan. 1st, 2026 to Jan. 7th, 2026",
        "from 1 January 2026 to 7 January 2026",
        "from 1 Sept 2026 to 7 September 2026",
        "from 2026-01-01 to 2026-01-07",
        "from 2026/1/1 to 2026/1/7",
        "on February 29, 2024",
    ])
    def test_explicit_valid_dates_satisfy_time_range(self, expression: str, intent: str) -> None:
        """完整有效日期已提供范围，TaskPlan 不应再阻断生成 SQL。"""
        from src.graph.contracts import build_task_plan

        # Arrange
        query = f"Show daily regional sales {expression}"
        # Act
        plan = build_task_plan(intent, query=query)
        # Assert
        assert plan.needs_time_range is False
        assert plan.missing_inputs == []

    # 验证非法日期、标识符和不完整日期不冒充有效时间范围。
    # Args: self - 实例；query - 非法或缺少日期的问题。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", [
        "", "Show regional sales", "Show sales from 2026-02-30 to 2026-02-31",
        "Show sales on February 29, 2025", "Show sales on Jan 0, 2026",
        "Show sales on April 31, 2026", "Show sales on 2026/13/01",
        "Compare code prefix2026-01-01suffix", "Show sales on January 7",
    ])
    def test_invalid_or_incomplete_dates_require_time_range(self, query: str) -> None:
        """不猜测年份，也不因非法日期或编码误放行趋势查询。"""
        from src.graph.contracts import build_task_plan

        # Arrange / Act
        plan = build_task_plan("trend", query=query)
        # Assert
        assert plan.needs_time_range is True
        assert plan.missing_inputs == ["time_range"]

    # 验证既有相对时间规则和无时间要求的能力保持不变。
    # Args: self - 实例；query - 原有相对时间表达。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", ["最近七天销售", "全部历史销售", "Sales last week", "Sales this quarter"])
    def test_date_fix_keeps_relative_range_fallback(self, query: str) -> None:
        """精确日期补充不能破坏相对时间或直接问答的路由。"""
        from src.graph.contracts import build_task_plan

        # Arrange / Act
        trend = build_task_plan("trend", query=query)
        direct = build_task_plan("chat", query="Hello")
        # Assert
        assert trend.needs_time_range is False
        assert direct.needs_time_range is False
        assert direct.capability == "direct_answer"

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
