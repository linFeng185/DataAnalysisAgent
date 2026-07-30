"""图回归、Schema 召回和 LangSmith 开关测试。"""

from __future__ import annotations

import pytest


class TestGraphEvaluation:
    """覆盖功能 15.4、15.5、15.7 的离线与远程评测边界。"""

    def test_fixed_graph_benchmark_passes_all_contracts(self) -> None:
        """固定基准中的路由、安全和 Artifact 契约必须全部通过。"""
        # Arrange
        from tests.evaluators.run_eval import run_offline

        # Act
        result = run_offline("tests/fixtures/graph_benchmark.json")

        # Assert
        assert result["case_count"] >= 12
        assert result["failed"] == 0
        assert result["pass_rate"] == 1.0

    def test_schema_recall_reports_partial_and_complete_hits(self) -> None:
        """Recall@K 应按期望表集合计算，并区分部分命中与完整命中。"""
        # Arrange
        from tests.evaluators.schema_recall import evaluate_schema_recall

        cases = [
            {"case_id": "orders", "query": "订单金额", "expected_tables": ["orders"]},
            {"case_id": "items", "query": "品类销售", "expected_tables": ["order_items", "products"]},
        ]
        retrieved = {
            "orders": ["orders", "customers"],
            "items": ["products", "customers"],
        }

        # Act
        result = evaluate_schema_recall(cases, retrieved, top_k=2)

        # Assert
        assert result["recall_at_k"] == 0.75
        assert result["complete_hit_rate"] == 0.5

    async def test_langsmith_evaluation_requires_explicit_switch(self, monkeypatch) -> None:
        """日常测试不得意外上传数据或调用远程 LangSmith。"""
        # Arrange
        from tests.evaluators.run_eval import run_langsmith_evaluation

        monkeypatch.delenv("RUN_LANGSMITH_EVALS", raising=False)

        async def target(inputs):
            return inputs

        # Act / Assert
        with pytest.raises(RuntimeError, match="RUN_LANGSMITH_EVALS"):
            await run_langsmith_evaluation(target, dataset_name="production")
