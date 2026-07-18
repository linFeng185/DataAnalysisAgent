"""知识检索离线评测指标测试。"""

from __future__ import annotations

from src.knowledge.retrieval_eval import evaluate_retrieval_cases


class TestRetrievalEvaluation:
    """覆盖 Recall@K、MRR、引用命中和越权召回。"""

    def test_evaluate_cases_calculates_quality_and_security_metrics(self):
        """命中、无答案和越权结果应分别体现在评测报告中。"""
        # Arrange
        cases = [
            {"query": "GMV", "relevant_ids": ["doc-1"], "authorized_ids": ["doc-1", "doc-2"]},
            {"query": "无答案", "relevant_ids": [], "authorized_ids": ["doc-3"]},
            {"query": "越权", "relevant_ids": ["doc-4"], "authorized_ids": ["doc-4"]},
        ]
        retrieved = {
            "GMV": ["doc-2", "doc-1"],
            "无答案": ["doc-3"],
            "越权": ["doc-9"],
        }

        # Act
        report = evaluate_retrieval_cases(cases, retrieved, top_k=2)

        # Assert
        assert report.recall_at_k == 1 / 3
        assert report.mrr_at_k == 1 / 4
        assert report.citation_hit_rate == 1 / 2
        assert report.unauthorized_hits == 1
        assert report.case_count == 3
