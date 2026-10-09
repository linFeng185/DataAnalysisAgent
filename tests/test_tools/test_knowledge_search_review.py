"""自主检索独立审核回归：覆盖功能27.1.2/27.1.3的授权、脱敏与故障预算。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from src.knowledge.asset_models import Evidence
from src.security.tenant_policy import RequestIdentity


class TestKnowledgeSearchReview:
    """覆盖27.1.2/27.1.3，不依赖真实模型或生产向量库。"""

    # 验证默认数据源不能绕过请求白名单。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言越权在检索前阻断。
    async def test_default_datasource_cannot_bypass_request_scope(self, monkeypatch) -> None:
        """默认源不在请求授权集合时必须拒绝，而非信任服务端字符串。"""
        import src.tools.knowledge_search as tools

        # Arrange：默认数据源故意与请求白名单冲突。
        identity = RequestIdentity(tenant_id=2, user_id=7, role="analyst")
        retrieval = AsyncMock(return_value=[])
        monkeypatch.setattr("src.knowledge.retrieval.search_knowledge", retrieval)
        monkeypatch.setattr(tools, "resolve_datasource_authorization", AsyncMock(return_value=True))
        tool = tools.build_knowledge_tools(
            identity=identity, default_datasource="restricted",
            allowed_datasources=["allowed"], store_provider=AsyncMock(return_value=object()),
        )[0]

        # Act：模型未显式指定源，也不能放宽服务端范围。
        result = await tools.execute_knowledge_search(tool, {"query": "status"}, identity=identity)

        # Assert：不向向量库提交任何查询。
        assert result["status"] == "forbidden"
        retrieval.assert_not_awaited()

    # 验证安全locator不会在渲染正文中被原始metadata重新引入。
    # Args: 无。
    # Returns: 无，断言所有输出字段均不含内部路径。
    def test_rendered_content_does_not_reintroduce_private_locator(self) -> None:
        """顶层locator脱敏后，可信知识XML同样不得含原始内部路径。"""
        import src.tools.knowledge_search as tools

        # Arrange：路径仅存在于定位和来源元数据，而非业务正文。
        evidence = Evidence(
            content="status的合法值为paid和cancelled", source_id="doc-review", version="1",
            locator={"page": 1, "file_path": "C:/private/review-secret.txt"},
            metadata={"source_file": "C:/private/review-source.txt"},
        )

        # Act：走真实证据渲染实现。
        rendered = tools._render_hits([evidence])

        # Assert：整个输出不能通过content字段再次泄漏。
        serialized = json.dumps(rendered, ensure_ascii=False)
        assert "review-secret.txt" not in serialized
        assert "review-source.txt" not in serialized

    # 验证向量库初始化属于单次检索超时范围。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言初始化超时不会进入实际检索。
    async def test_store_initialization_is_inside_retrieval_deadline(self, monkeypatch) -> None:
        """冷启动超过工具时间预算必须返回timeout，不得只限制search阶段。"""
        import src.tools.knowledge_search as tools

        # Arrange：用短延迟模拟慢初始化，不访问实际服务。
        identity = RequestIdentity(tenant_id=2, user_id=7, role="analyst")
        retrieval = AsyncMock(return_value=[])
        monkeypatch.setattr("src.knowledge.retrieval.search_knowledge", retrieval)

        # 模拟在获取向量库时发生可取消的等待。
        # Args: 无。
        # Returns: 用于离线检索的占位对象。
        async def delayed_store() -> object:
            await asyncio.sleep(0.05)
            return object()

        tool = tools.build_knowledge_tools(
            identity=identity, store_provider=delayed_store, per_retrieval_seconds=0.01,
        )[0]

        # Act：工具期限应覆盖整个检索操作。
        result = await tools.execute_knowledge_search(tool, {"query": "status"}, identity=identity)

        # Assert：初始化耗尽预算后禁止继续查库。
        assert result["status"] == "timeout"
        retrieval.assert_not_awaited()

    # 验证失败尝试同样消耗有界检索预算。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言第二次请求被预算阻断。
    async def test_failed_retrieval_consumes_attempt_budget(self, monkeypatch) -> None:
        """服务故障不得绕过次数限制导致MCP不断重试检索。"""
        import src.tools.knowledge_search as tools

        # Arrange：一次实际失败后就必须达到次数上限。
        identity = RequestIdentity(tenant_id=2, user_id=7, role="analyst")
        retrieval = AsyncMock(side_effect=RuntimeError("synthetic backend failure"))
        monkeypatch.setattr("src.knowledge.retrieval.search_knowledge", retrieval)
        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: True)
        tool = tools.build_knowledge_tools(
            identity=identity, store_provider=AsyncMock(return_value=object()), max_retrievals=1,
        )[0]

        # Act：相同失败不应被缓存，也不能绕过实际尝试预算。
        first = await tools.execute_knowledge_search(tool, {"query": "status"}, identity=identity)
        second = await tools.execute_knowledge_search(tool, {"query": "status"}, identity=identity)

        # Assert：第一次明确不可用，第二次耗尽预算。
        assert first["status"] == "unavailable"
        assert second["status"] == "budget_exhausted"
        assert retrieval.await_count == 1

    # 验证真实检索层的分范围故障不会被工具误报为空知识。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言全范围不可用时返回unavailable。
    async def test_real_retrieval_backend_failure_is_not_empty_knowledge(self, monkeypatch) -> None:
        """保留真实search_knowledge，只替换存储边界，区分无命中与服务故障。"""
        from types import SimpleNamespace

        import src.tools.knowledge_search as tools

        # Arrange：向量与关键词接口均不可用，但不Mock上层检索结果。
        identity = RequestIdentity(tenant_id=2, user_id=7, role="analyst")
        store = SimpleNamespace(
            search=AsyncMock(side_effect=ConnectionError("synthetic vector backend unavailable")),
            get_by_filter=AsyncMock(side_effect=ConnectionError("synthetic lexical backend unavailable")),
        )
        monkeypatch.setattr("src.failure_policy.fallback_allowed", lambda domain: True)
        tool = tools.build_knowledge_tools(identity=identity, store_provider=AsyncMock(return_value=store))[0]

        # Act：执行完整工具→安全检索→存储边界。
        result = await tools.execute_knowledge_search(tool, {"query": "status"}, identity=identity)

        # Assert：不能误导模型为知识库不存在口径。
        assert result["status"] == "unavailable"
        assert store.search.await_count >= 1
