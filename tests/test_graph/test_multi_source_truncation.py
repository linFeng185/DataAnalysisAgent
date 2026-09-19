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
            (300, False, False, False),
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
        """来源行只受全局配置上限约束；来源真实截断才写 truncated。"""
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

    # 方法作用：验证单源结果行不被 Prompt 预算改写，同时分别保留两类裁剪信号。
    # Args: self - 测试实例；monkeypatch - 补丁工具；row_count - 单源返回行数；flag - 上游截断标记。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("row_count", "flag"), [(90, False), (50, False), (2, True)])
    async def test_worker_preserves_rows_and_prompt_cap(self, monkeypatch: pytest.MonkeyPatch, row_count: int, flag: bool) -> None:
        """Prompt 只读取少量样例；来源 data 保留执行层返回的完整有界行集。"""
        # Arrange
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=SimpleNamespace(schema=None, dialect="oracle")))
        monkeypatch.setattr("src.datasource.registry.get_registry", Mock(return_value=registry))
        subgraph = SimpleNamespace(ainvoke=AsyncMock(return_value={"generated_sql": "SELECT 1 FROM dual", "query_result_sample": [{"amount": index} for index in range(row_count)], "query_result_full_count": row_count, "query_result_statistics": {"truncated": flag}}))

        # Act
        result = await _analyze_one("oracle", {"user_query": "查询明细", "tenant_id": 3}, subgraph=subgraph)

        # Assert
        assert len(result["data"]) == row_count
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


class TestMergedSqlEvidence:
    """覆盖多源合并 SQL 从最终返回值到报表单元快照的传递。"""

    # 方法作用：验证多源合并的最终返回值携带每个成功来源的 SQL 与数据源标签。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_final_return_carries_merged_source_sql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """最终 return 丢 generated_sql 会让跨源单元的报表快照 sql_text 变空。"""
        # Arrange：只替身展示子图与 LLM，合并节点用真实实现。
        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=False))
        state = {"user_query": "比较各源销售额", "multi_source_results": [{"datasource": "oracle", "success": True, "sql": "SELECT sales FROM orders", "data": [{"sales": 12}]}, {"datasource": "ch", "success": True, "sql": "SELECT attributed FROM attribution", "data": [{"attributed": 18}]}, {"datasource": "mysql", "success": False, "error": "连接失败"}]}

        # Act
        result = await merge_results_node(state)

        # Assert：成功来源按 `-- 数据源` 标签拼接，失败来源不得混入。
        assert result["generated_sql"] == "-- oracle\nSELECT sales FROM orders\n\n-- ch\nSELECT attributed FROM attribution"
        assert presenter.ainvoke.call_args.args[0]["generated_sql"] == result["generated_sql"]

    # 方法作用：验证没有可展示 SQL 时最终返回值给空串而不是只有数据源标签的占位文本。
    # Args: self - 测试实例；monkeypatch - 补丁工具；results - 多源调度结果明细。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "results",
        [
            [{"datasource": "oracle", "success": False, "error": "连接失败"}],
            [{"datasource": "oracle", "success": True, "sql": "", "data": [{"sales": 12}]}, {"datasource": "ch", "success": True, "data": [{"sales": 18}]}],
        ],
    )
    async def test_no_source_sql_yields_empty_text(self, monkeypatch: pytest.MonkeyPatch, results: list[dict]) -> None:
        """全失败或各源 SQL 均空时，快照的 has_sql 不能被数据源标签误判成有 SQL。"""
        # Arrange
        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=False))
        state = {"user_query": "比较各源销售额", "multi_source_results": results}

        # Act
        result = await merge_results_node(state)

        # Assert
        assert result["generated_sql"] == ""


class TestMergedRowsFollowReturnedRows:
    """回归：跨源合并行集必须跟随来源真实回传行集，提示词预算无权决定它。

    事故：库里是 2016–2036 共 20 年数据、生成的 SQL 里没有任何 LIMIT、
    `truncated=False`，但跨源单元只收到 100 行——两个来源各被 `_PROMPT_SAMPLE_ROWS=50`
    提前裁过。分析结论因此写成「样本仅覆盖 1-2 月」「无法给出全年趋势与同比」。
    本类锁定「提示词预算只表示喂给模型的样本被裁」这一事实，不得再改写 data、
    合并行集与 full_count；只贴 sample_capped 标签而数据通路照旧时，下面必红。
    """

    # 方法作用：构造返回指定行数与全量计数的单源 SQL 子图替身。
    # Args: rows - 子图返回的样本行；full_count - 子图上报的查询返回计数。
    # Returns: 可注入 _analyze_one 的子图替身。
    @staticmethod
    def _worker_subgraph(rows: list[dict], full_count: int) -> SimpleNamespace:
        return SimpleNamespace(ainvoke=AsyncMock(return_value={
            "generated_sql": "SELECT stat_month, net_sales FROM sales ORDER BY stat_month",
            "query_result_sample": rows,
            "query_result_full_count": full_count,
        }))

    # 方法作用：构造连续 300 个日历月标签，覆盖 25 年跨度。
    # Args: 无。
    # Returns: "YYYY-MM" 标签列表。
    @staticmethod
    def _month_labels() -> list[str]:
        return [f"{2016 + index // 12:04d}-{index % 12 + 1:02d}" for index in range(300)]

    # 方法作用：验证两源各 300 行时合并行集接近 600 行，而不是提示词预算的 100 行。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_merged_sample_follows_returned_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """修复前：每源 data 被提前裁到 50，合并后 100 行、full_count 也是 100。"""
        # Arrange：两个来源各返回 300 行，其中一源全量计数远大于回传行集
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=SimpleNamespace(schema=None, dialect="oracle")))
        monkeypatch.setattr("src.datasource.registry.get_registry", Mock(return_value=registry))
        labels = self._month_labels()
        rows = [{"stat_month": label, "net_sales": index} for index, label in enumerate(labels)]
        source_a = await _analyze_one("oracle", {"user_query": "对比各源净销售额趋势", "tenant_id": 3}, subgraph=self._worker_subgraph(list(rows), 1200))
        source_b = await _analyze_one("ch", {"user_query": "对比各源净销售额趋势", "tenant_id": 3}, subgraph=self._worker_subgraph([dict(row) for row in rows], 300))
        presenter = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
        monkeypatch.setattr("src.graph.subgraphs.result_presentation.build_result_presentation_subgraph", Mock(return_value=presenter))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(return_value=False))

        # Act
        merged = await merge_results_node({"user_query": "对比各源净销售额趋势", "multi_source_results": [source_a, source_b]})

        # Assert：来源级不再被提示词预算改写，但「提示词样本被裁」这一事实照旧上报
        assert len(source_a["data"]) == 300
        assert source_a["full_count"] == 1200
        assert source_a.get("sample_capped", False) is True
        # 合并行集覆盖全部 300 个标签（含 2036 年的最新月份），不是只有最早两个月
        assert len(merged["query_result_sample"]) == 600
        assert {row["stat_month"] for row in merged["query_result_sample"]} == set(labels)
        assert merged["query_result_sample_capped"] is True
        # full_count 反映查询真实规模，而不是被裁后的可见行数
        assert merged["query_result_full_count"] == 1500
        assert presenter.ainvoke.call_args.args[0]["query_result_full_count"] == 1500
