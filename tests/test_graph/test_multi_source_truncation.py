"""多源样本裁剪信号回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.graph.nodes.multi_source import _analyze_one, merge_results_node


class TestMultiSourceTruncation:
    """覆盖 23.16 多源裁剪到报表质量证据的传递。"""

    # 方法作用：验证来源截断与合并裁剪均写回状态并先交给分析节点。
    # Args: self - 测试实例；monkeypatch - 补丁工具；row_count - 来源行数；source_truncated - 来源截断；expected - 预期截断。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("row_count", "source_truncated", "expected"), [(50, False, True), (2, True, True), (2, False, False)])
    async def test_merge_preserves_truncation(self, monkeypatch: pytest.MonkeyPatch, row_count: int, source_truncated: bool, expected: bool) -> None:
        """50→30 行以及来源已截断时，分析与最终出口都必须披露样本受限。"""
        # Arrange
        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=False))
        state = {"user_query": "查看各地区明细", "multi_source_results": [{"datasource": "oracle", "success": True, "truncated": source_truncated, "data": [{"region": f"区域{index}", "amount": index} for index in range(row_count)]}]}

        # Act
        result = await merge_results_node(state)

        # Assert
        assert result["query_result_truncated"] is expected
        assert result["query_result_statistics"]["truncated"] is expected
        assert presenter.ainvoke.call_args.args[0]["query_result_truncated"] is expected

    # 方法作用：验证单源 SQL 返回样本再裁到 50 行时保留截断标记与原始计数。
    # Args: self - 测试实例；monkeypatch - 补丁工具；row_count - 单源返回行数；flag - 上游截断标记。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("row_count", "flag"), [(90, False), (50, False), (2, True)])
    async def test_worker_preserves_sample_cut(self, monkeypatch: pytest.MonkeyPatch, row_count: int, flag: bool) -> None:
        """重现日志中的 90→50 行，标记必须经 SourceQueryResult 序列化保留。"""
        # Arrange
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=SimpleNamespace(schema=None, dialect="oracle")))
        monkeypatch.setattr("src.datasource.registry.get_registry", Mock(return_value=registry))
        subgraph = SimpleNamespace(ainvoke=AsyncMock(return_value={"generated_sql": "SELECT 1 FROM dual", "query_result_sample": [{"amount": index} for index in range(row_count)], "query_result_full_count": row_count, "query_result_statistics": {"truncated": flag}}))

        # Act
        result = await _analyze_one("oracle", {"user_query": "查询明细", "tenant_id": 3}, subgraph=subgraph)

        # Assert
        assert len(result["data"]) == min(50, row_count)
        assert result["full_count"] == row_count
        assert result.get("truncated", False) is (row_count > 50 or flag)
