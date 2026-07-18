"""知识证据确定性重排测试。"""

from __future__ import annotations

from src.knowledge.asset_models import Evidence
from src.knowledge.reranker import rerank_evidence


class TestLightweightReranker:
    """覆盖精确短语、词法分数和来源多样性。"""

    def test_rerank_promotes_exact_phrase_and_penalizes_duplicate_source(self):
        """精确命中应优先，同一原文连续重复应适度降权。"""
        # Arrange
        evidence = [
            Evidence(content="GMV 指标定义和口径", source_id="a", version="v1",
                     locator={"paragraph": 1}, scores={"vector": 0.85, "lexical": 0.1},
                     metadata={"source_file": "metrics.md"}),
            Evidence(content="orders.amount 是订单金额字段", source_id="b", version="v2",
                     locator={"column": "amount"}, scores={"vector": 0.65, "lexical": 0.8},
                     metadata={"source_file": "schema.md", "column_name": "amount"}),
            Evidence(content="GMV 旧版定义", source_id="c", version="v1",
                     locator={"paragraph": 2}, scores={"vector": 0.8, "lexical": 0.2},
                     metadata={"source_file": "metrics.md"}),
        ]

        # Act
        result = rerank_evidence(evidence, "订单金额 amount", top_k=3)

        # Assert
        assert result[0].source_id == "b"
        assert all("rerank" in item.scores for item in result)
        assert result[2].scores["rerank"] < result[0].scores["rerank"]
