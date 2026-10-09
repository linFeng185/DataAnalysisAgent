"""功能27.1.2/27.1.8独立验收：真实隔离Chroma、工具循环和跨方言SQL校验。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.memory.vector_store import VectorEntry
from src.security.tenant_policy import RequestIdentity, TenantPolicy
from tests.test_graph.test_knowledge_sql_cross_dialect import _FakeToolModel, _prepared


class TestKnowledgeChromaReview:
    """不用无条件返回结果的存储替身，验证真实metadata过滤与证据链路。"""

    # 验证真实Chroma隔离过滤后的知识可以进入模型续轮并通过SQL校验。
    # Args: monkeypatch - 测试替换工具；chroma_store - 隔离内存Chroma；dialect - SQL方言。
    # Returns: 无，断言跨租户和他人私有知识均不进入模型消息。
    @pytest.mark.integration
    @pytest.mark.parametrize("dialect", ["clickhouse", "mysql", "postgres"])
    async def test_real_chroma_evidence_is_authorized_and_sql_valid(
        self, monkeypatch, chroma_store, dialect: str,
    ) -> None:
        """真实向量适配器按租户/可见性过滤，模型只收到本租户口径与真实引用。"""
        from src.api.auth import use_identity_context
        from src.llm.knowledge_loop import (
            KnowledgeLoopConfig,
            invoke_with_knowledge,
            use_knowledge_loop,
        )
        from src.security.sql_schema_validation import validate_sql_schema
        from src.tools.sqlglot_validator import validate_with_sqlglot

        # Arrange：三个相似度相同的文档，只有一个在当前身份可见范围内。
        base_metadata = {
            "datasource": "", "category": "enum_value", "source": "manual_doc",
            "table_name": "orders", "column_name": "status", "document_version": "v3",
        }
        await chroma_store.upsert([
            VectorEntry(
                id="authorized-enum", content="orders.status 合法值为 paid 和 cancelled",
                metadata={**base_metadata, "tenant_id": 2, "visibility": "tenant", "owner_user_id": 7},
                embedding=[1.0, 0.0, 0.0, 0.0],
            ),
            VectorEntry(
                id="foreign-enum", content="orders.status 合法值为 foreign_secret",
                metadata={**base_metadata, "tenant_id": 3, "visibility": "tenant", "owner_user_id": 7},
                embedding=[1.0, 0.0, 0.0, 0.0],
            ),
            VectorEntry(
                id="private-enum", content="orders.status 合法值为 other_user_secret",
                metadata={**base_metadata, "tenant_id": 2, "visibility": "private", "owner_user_id": 9},
                embedding=[1.0, 0.0, 0.0, 0.0],
            ),
        ])
        monkeypatch.setattr("src.knowledge.retrieval.get_tenant_policy", lambda: TenantPolicy(multi_tenant=True))
        identity = RequestIdentity(tenant_id=2, user_id=7, role="analyst")
        sql = "SELECT status, COUNT(*) FROM orders WHERE status IN ('paid','cancelled') GROUP BY status"
        model = _FakeToolModel(sql)
        config = KnowledgeLoopConfig(
            identity=identity, allowed_datasources=frozenset(),
            store_provider=AsyncMock(return_value=chroma_store),
        )

        # Act：只有模型是Fake；工具、安全检索、重排与Chroma均走真实实现。
        with use_identity_context(identity), use_knowledge_loop(config):
            response = await invoke_with_knowledge(_prepared(model))

        # Assert：模型收到完整工具回复，且只有真实授权证据。
        assert len(model.calls) == 2
        replies = [message for message in model.calls[1]["messages"] if getattr(message, "type", "") == "tool"]
        payload = json.loads(replies[0].content)
        assert payload["status"] == "ok"
        assert {item["source_id"] for item in payload["citations"]} == {"authorized-enum"}
        assert "foreign_secret" not in replies[0].content
        assert "other_user_secret" not in replies[0].content
        assert "paid" in replies[0].content and "cancelled" in replies[0].content
        assert validate_with_sqlglot(response.content, dialect)["valid"] is True
        issues = validate_sql_schema(
            response.content, datasources=["orders"], dialect=dialect,
            tables=[{"name": "orders", "datasource": "orders", "columns": [{"name": "status"}]}],
        )
        assert not [issue for issue in issues if issue.confidence == "high"]
