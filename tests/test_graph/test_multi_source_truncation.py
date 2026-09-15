"""多源样本裁剪信号回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.graph.nodes.multi_source import (
    _analyze_one,
    _normalize_cross_source_rows,
    merge_results_node,
)


class TestMultiSourceTruncation:
    """覆盖 23.16 多源裁剪到报表质量证据的传递。"""

    # 方法作用：验证同位置且同为数值的异义指标不会被改成首源字段名。
    # Args: self - 测试实例；left - 第一源字段；right - 第二源字段。
    # Returns: 无返回值。
    @pytest.mark.parametrize(("left", "right"), [("net_sales_amount", "market_index_avg"), ("order_count", "attributed_revenue"), ("avg_order_value", "avg_air_quality_index"), ("stat_month", "stat_date")])
    def test_different_metrics_never_align_by_position(self, left: str, right: str) -> None:
        """外部景气/空气质量指数不能因列数和类型一致被伪装成销售指标。"""
        # Arrange
        results = [{"datasource": "mysql", "data": [{left: 10}]}, {"datasource": "ch", "data": [{right: 99}]}]

        # Act
        aligned = _normalize_cross_source_rows(results)

        # Assert
        assert aligned is None
        assert results[1]["data"] == [{right: 99}]

    # 方法作用：验证来源真实截断与每源贡献上限分别披露，不再混成一个 truncated。
    # Args: self - 测试实例；monkeypatch - 补丁工具；row_count - 来源行数；source_truncated - 来源截断；
    #       expected_truncated - 预期真实截断；expected_sample_capped - 预期样本受限。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("row_count", "source_truncated", "expected_truncated", "expected_sample_capped"),
        [
            (300, False, False, True),
            (2, True, True, False),
            (2, False, False, False),
        ],
    )
    async def test_merge_preserves_truncation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        row_count: int,
        source_truncated: bool,
        expected_truncated: bool,
        expected_sample_capped: bool,
    ) -> None:
        """300 行超过每源贡献上限 200 只算样本受限；来源真实截断才写 truncated。"""
        # Arrange
        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=False))
        state = {"user_query": "查看各地区明细", "multi_source_results": [{"datasource": "oracle", "success": True, "truncated": source_truncated, "data": [{"region": f"区域{index}", "amount": index} for index in range(row_count)]}]}

        # Act
        result = await merge_results_node(state)

        # Assert
        assert result["query_result_truncated"] is expected_truncated
        assert result["query_result_statistics"]["truncated"] is expected_truncated
        assert result["query_result_sample_capped"] is expected_sample_capped
        assert presenter.ainvoke.call_args.args[0]["query_result_truncated"] is expected_truncated

    # 方法作用：验证单源 SQL 返回样本再裁到 50 行时，只标样本受限而不冒充真实截断。
    # Args: self - 测试实例；monkeypatch - 补丁工具；row_count - 单源返回行数；flag - 上游截断标记。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("row_count", "flag"), [(90, False), (50, False), (2, True)])
    async def test_worker_preserves_sample_cut(self, monkeypatch: pytest.MonkeyPatch, row_count: int, flag: bool) -> None:
        """重现日志中的 90→50 行：提示词样本被裁记 sample_capped，真实截断才记 truncated。"""
        # Arrange
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=SimpleNamespace(schema=None, dialect="oracle")))
        monkeypatch.setattr("src.datasource.registry.get_registry", Mock(return_value=registry))
        subgraph = SimpleNamespace(ainvoke=AsyncMock(return_value={"generated_sql": "SELECT 1 FROM dual", "query_result_sample": [{"amount": index} for index in range(row_count)], "query_result_full_count": row_count, "query_result_statistics": {"truncated": flag}}))

        # Act
        result = await _analyze_one("oracle", {"user_query": "查询明细", "tenant_id": 3}, subgraph=subgraph)

        # Assert
        assert len(result["data"]) == min(50, row_count)
        assert result["full_count"] == row_count
        assert result.get("truncated", False) is flag
        assert result.get("sample_capped", False) is (row_count > 50)

    # 方法作用：验证截断后有界结果的分析摘要不被之前仅看三行的合并摘要覆盖。
    # Args: self - 测试实例；monkeypatch - 补丁工具；truncated - 是否发生来源裁剪。
    # Returns: 无返回值。
    @pytest.mark.parametrize("truncated", [True, False])
    async def test_truncated_analysis_keeps_final_evidence_summary(self, monkeypatch: pytest.MonkeyPatch, truncated: bool) -> None:
        """旧合并摘要的 50 行口径不能覆盖已按 30 行样本重新分析的结果。"""
        # Arrange
        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={"analysis_result": {"summary": "基于最终30行截断样本", "limitations": ["样本不完整"]}}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=True))
        monkeypatch.setattr("src.llm.invocation.invoke_text", AsyncMock(return_value='{"summary":"只有三行可见的旧结论"}'))
        state = {"user_query": "查看明细", "multi_source_results": [{"datasource": "oracle", "success": True, "truncated": truncated, "data": [{"amount": index} for index in range(12)]}]}

        # Act
        result = await merge_results_node(state)

        # Assert
        assert result["analysis_result"]["summary"] == "基于最终30行截断样本"

    # 方法作用：验证最终展示子图收到来源 SQL 与来源计数，避免只看数据猜测口径。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_presentation_preserves_source_execution_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """跨源归因收入和订单净额不能在最终分析时丢掉各自执行来源。"""
        # Arrange
        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=False))
        state = {"user_query": "比较广告归因与销售", "multi_source_results": [{"datasource": "oracle", "success": True, "sql": "SELECT sales FROM orders", "full_count": 90, "truncated": True, "data": [{"sales": 1}]}, {"datasource": "ch", "success": True, "sql": "SELECT attributed_revenue FROM attribution", "full_count": 20, "data": [{"attributed_revenue": 2}]}]}

        # Act
        await merge_results_node(state)

        # Assert
        received = presenter.ainvoke.call_args.args[0]
        assert "SELECT attributed_revenue FROM attribution" in received["generated_sql"]
        assert received["query_result_full_count"] == 110
        assert "oracle" in received["business_rules_text"] and "ch" in received["business_rules_text"]
