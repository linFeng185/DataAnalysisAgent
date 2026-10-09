"""只读知识检索工具测试，覆盖功能 27.1.1 与 27.1.2。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from src.knowledge.asset_models import Evidence
from src.security.tenant_policy import RequestIdentity


# 方法作用：构造一条可用的知识证据，避免每个用例重复拼装。
# Args: source_id - 证据标识；content - 正文；kwargs - 追加的 metadata 字段与 locator。
# Returns: 示例 Evidence。
def _evidence(source_id: str = "doc-1", content: str = "GMV 口径为已支付订单金额", **kwargs) -> Evidence:
    return Evidence(
        content=content,
        source_id=source_id,
        version="v1",
        locator=kwargs.pop("locator", {"page": 1}),
        scores={"relevance": 0.9, "fused": 0.8},
        metadata={"category": "metric", "source": "manual_doc", **kwargs},
    )


# 方法作用：构造一个不依赖真实认证中间件的请求身份。
# Args: user_id - 用户 ID；role - 角色；tenant_id - 租户 ID。
# Returns: RequestIdentity。
def _identity(user_id: int = 7, role: str = "analyst", tenant_id: int = 2) -> RequestIdentity:
    return RequestIdentity(tenant_id=tenant_id, user_id=user_id, role=role, username="tester")


class TestKnowledgeSearchArgs:
    """覆盖功能 27.1.1 的参数契约：拒绝身份、权限和未知字段。"""

    # 方法作用：模型不得通过工具参数传递 tenant_id、user_id、role 或 filters。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_args_reject_identity_and_unknown_fields(self) -> None:
        """模型不得通过工具参数传递 tenant_id、user_id、role 或 filters。"""
        # Arrange
        from src.tools.knowledge_search import KnowledgeSearchArgs

        # Act + Assert
        for payload in (
            {"query": "GMV", "tenant_id": 3},
            {"query": "GMV", "user_id": 9},
            {"query": "GMV", "role": "super_admin"},
            {"query": "GMV", "visibility": "system"},
            {"query": "GMV", "owner_user_id": 1},
            {"query": "GMV", "filters": {"tenant_id": 1}},
            {"query": "GMV", "unbound_only": True},
        ):
            try:
                KnowledgeSearchArgs(**payload)
            except ValidationError:
                continue
            raise AssertionError(f"非法字段未被拒绝: {payload}")

    # 方法作用：空白查询被拒绝，超长与越界值被拒绝，合法值被规范化。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_args_normalize_and_bound_values(self) -> None:
        """空白查询被拒绝，超长与越界值被拒绝，合法值被规范化。"""
        # Arrange
        from src.tools.knowledge_search import KnowledgeSearchArgs

        # Act
        parsed = KnowledgeSearchArgs(
            query="  orders.status 枚举 ", datasource=" orders ", category=" metric ", top_k=3,
        )

        # Assert
        assert parsed.query == "orders.status 枚举"
        assert parsed.datasource == "orders"
        assert parsed.category == "metric"
        assert parsed.top_k == 3
        assert KnowledgeSearchArgs(query="GMV").top_k == 5
        assert KnowledgeSearchArgs(query="GMV").datasource == ""

        for payload in (
            {"query": "   "},
            {"query": "x" * 513},
            {"query": "GMV", "datasource": "d" * 129},
            {"query": "GMV", "category": "c" * 65},
            {"query": "GMV", "top_k": 0},
            {"query": "GMV", "top_k": 9},
        ):
            try:
                KnowledgeSearchArgs(**payload)
            except ValidationError:
                continue
            raise AssertionError(f"非法参数未被拒绝: {payload}")


class TestKnowledgeSearchExecution:
    """覆盖功能 27.1.2 的身份复核、授权和未绑定过滤。"""

    # 方法作用：匿名身份不构建检索工具。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_build_tools_requires_trusted_identity(self) -> None:
        """匿名身份不构建检索工具。"""
        # Arrange
        import src.tools.knowledge_search as module

        # Act
        tools = module.build_knowledge_tools(identity=RequestIdentity.anonymous())

        # Assert
        assert tools == []

    # 方法作用：无默认数据源且参数为空时只查明确未绑定知识，不做全源扫描。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_pushes_unbound_only_without_default_datasource(self, monkeypatch) -> None:
        """无默认数据源且参数为空时只查明确未绑定知识，不做全源扫描。"""
        # Arrange
        import src.tools.knowledge_search as module

        captured: dict[str, object] = {}

        # 方法作用：覆盖 fake_search 的测试场景
        # Args: store - 用例参数；query - 用例参数
        # Returns: 见方法内返回
        async def fake_search(store, query, **kwargs):
            captured.update(kwargs)
            return [_evidence()]

        monkeypatch.setattr(module, "search_knowledge", fake_search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )
        assert len(tools) == 1

        # Act
        payload = await module.execute_knowledge_search(
            tools[0],
            {"query": "GMV 口径"},
            identity=_identity(),
        )

        # Assert
        assert payload["status"] == "ok"
        assert captured["unbound_only"] is True
        assert captured["datasource"] == ""

    # 方法作用：显式类别与 top_k 必须下推到检索层。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_forwards_category_and_top_k(self, monkeypatch) -> None:
        """显式类别与 top_k 必须下推到检索层。"""
        # Arrange
        import src.tools.knowledge_search as module

        captured: dict[str, object] = {}

        # 方法作用：覆盖 fake_search 的测试场景
        # Args: store - 用例参数；query - 用例参数
        # Returns: 见方法内返回
        async def fake_search(store, query, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(module, "search_knowledge", fake_search, raising=False)
        # 默认数据源同样要过服务端授权，这里用显式授权替身避免触碰真实 Registry。
        monkeypatch.setattr(
            module, "resolve_datasource_authorization", AsyncMock(return_value=True), raising=False,
        )
        tools = module.build_knowledge_tools(
            identity=_identity(),
            default_datasource="orders",
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0],
            {"query": "status 枚举", "category": "enum_value", "top_k": 2},
            identity=_identity(),
        )

        # Assert
        assert payload["status"] == "empty"
        assert payload["evidence"] == []
        assert captured["datasource"] == "orders"
        assert captured["category"] == "enum_value"
        assert captured["top_k"] == 2

    # 方法作用：构建时与执行时身份不一致必须失败关闭，且不触发检索。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_denies_identity_drift(self, monkeypatch) -> None:
        """构建时与执行时身份不一致必须失败关闭，且不触发检索。"""
        # Arrange
        import src.tools.knowledge_search as module

        store_provider = AsyncMock(return_value=SimpleNamespace())
        tools = module.build_knowledge_tools(identity=_identity(user_id=7), store_provider=store_provider)

        # Act
        payload = await module.execute_knowledge_search(
            tools[0],
            {"query": "GMV"},
            identity=_identity(user_id=8),
        )

        # Assert
        assert payload["status"] == "forbidden"
        assert payload["evidence"] == []
        store_provider.assert_not_awaited()

    # 方法作用：显式数据源不在本次请求白名单内时必须拒绝。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_denies_datasource_outside_request_whitelist(self, monkeypatch) -> None:
        """显式数据源不在本次请求白名单内时必须拒绝。"""
        # Arrange
        import src.tools.knowledge_search as module

        authorization = AsyncMock(return_value=True)
        monkeypatch.setattr(module, "resolve_datasource_authorization", authorization, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            allowed_datasources=frozenset({"orders"}),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0],
            {"query": "GMV", "datasource": "payments"},
            identity=_identity(),
        )

        # Assert
        assert payload["status"] == "forbidden"
        authorization.assert_not_awaited()

    # 方法作用：白名单缺失时仍需通过服务端数据源授权。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_denies_unauthorized_datasource(self, monkeypatch) -> None:
        """白名单缺失时仍需通过服务端数据源授权。"""
        # Arrange
        import src.tools.knowledge_search as module

        monkeypatch.setattr(
            module,
            "resolve_datasource_authorization",
            AsyncMock(return_value=False),
            raising=False,
        )
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0],
            {"query": "GMV", "datasource": "payments"},
            identity=_identity(),
        )

        # Assert
        assert payload["status"] == "forbidden"

    # 方法作用：授权服务异常不能退化成无过滤检索。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_authorization_failure_does_not_fall_back(self, monkeypatch) -> None:
        """授权服务异常不能退化成无过滤检索。"""
        # Arrange
        import src.tools.knowledge_search as module

        # 方法作用：覆盖 broken 的测试场景
        # Args: 无
        # Returns: 见方法内返回
        async def broken(*args, **kwargs):
            raise RuntimeError("权限服务不可用")

        monkeypatch.setattr(module, "resolve_datasource_authorization", broken, raising=False)
        search = AsyncMock(return_value=[_evidence()])
        monkeypatch.setattr(module, "search_knowledge", search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0],
            {"query": "GMV", "datasource": "payments"},
            identity=_identity(),
        )

        # Assert
        assert payload["status"] == "forbidden"
        search.assert_not_awaited()

    # 方法作用：超时与存储不可用必须区分为不同状态，不能伪装成空结果。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_reports_timeout_and_unavailable(self, monkeypatch) -> None:
        """超时与存储不可用必须区分为不同状态，不能伪装成空结果。"""
        # Arrange
        import src.tools.knowledge_search as module

        # 方法作用：覆盖 slow_search 的测试场景
        # Args: store - 用例参数；query - 用例参数
        # Returns: 见方法内返回
        async def slow_search(store, query, **kwargs):
            await asyncio.sleep(5)
            return []

        monkeypatch.setattr(module, "search_knowledge", slow_search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            per_retrieval_seconds=0.05,
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0], {"query": "GMV"}, identity=_identity(),
        )

        # Assert
        assert payload["status"] == "timeout"

        # 方法作用：覆盖 broken_search 的测试场景
        # Args: store - 用例参数；query - 用例参数
        # Returns: 见方法内返回
        async def broken_search(store, query, **kwargs):
            raise RuntimeError("向量库连接失败")

        monkeypatch.setattr(module, "search_knowledge", broken_search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0], {"query": "GMV"}, identity=_identity(),
        )

        # Assert
        assert payload["status"] == "unavailable"

    # 方法作用：检索次数预算耗尽后不再访问向量库。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_reports_budget_exhausted(self, monkeypatch) -> None:
        """检索次数预算耗尽后不再访问向量库。"""
        # Arrange
        import src.tools.knowledge_search as module

        search = AsyncMock(return_value=[_evidence()])
        monkeypatch.setattr(module, "search_knowledge", search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            max_retrievals=1,
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        first = await module.execute_knowledge_search(tools[0], {"query": "A"}, identity=_identity())
        second = await module.execute_knowledge_search(tools[0], {"query": "B"}, identity=_identity())

        # Assert
        assert first["status"] == "ok"
        assert second["status"] == "budget_exhausted"
        assert search.await_count == 1

    # 方法作用：返回体只包含受限正文、安全 locator、来源标识与必要分数。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_result_exposes_only_safe_fields(self, monkeypatch) -> None:
        """返回体只包含受限正文、安全 locator、来源标识与必要分数。"""
        # Arrange
        import src.tools.knowledge_search as module

        evidence = _evidence(
            locator={"page": 3, "path": "C:/secret/conn.env", "dsn": "postgres://u:p@h/db"},
            connection_string="postgres://root:pw@10.0.0.1/db",
            owner_user_id=7,
        )
        monkeypatch.setattr(
            module, "search_knowledge", AsyncMock(return_value=[evidence]), raising=False,
        )
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0], {"query": "GMV"}, identity=_identity(),
        )

        # Assert
        assert payload["status"] == "ok"
        assert payload["truncated"] is False
        entry = payload["evidence"][0]
        assert entry["source_id"] == "doc-1"
        assert entry["version"] == "v1"
        assert entry["category"] == "metric"
        assert "GMV" in entry["content"]
        assert "trusted_knowledge" in entry["content"]
        assert set(entry["locator"]) == {"page"}
        assert "metadata" not in entry
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "connection_string" not in serialized
        assert "postgres://" not in serialized
        assert "owner_user_id" not in serialized
        assert payload["citations"] == [{
            "source_id": "doc-1",
            "version": "v1",
            "locator": {"page": 3},
        }]

    # 方法作用：超出总字符预算时按整条证据裁减，保持 JSON 完整可解析。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_truncates_evidence_without_breaking_json(self, monkeypatch) -> None:
        """超出总字符预算时按整条证据裁减，保持 JSON 完整可解析。"""
        # Arrange
        import src.tools.knowledge_search as module

        bulky = [
            _evidence(source_id=f"doc-{index}", content="口径说明" * 400)
            for index in range(6)
        ]
        monkeypatch.setattr(
            module, "search_knowledge", AsyncMock(return_value=bulky), raising=False,
        )
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0], {"query": "GMV", "top_k": 6}, identity=_identity(),
        )

        # Assert
        assert payload["status"] == "ok"
        assert payload["truncated"] is True
        assert 0 < len(payload["evidence"]) < len(bulky)
        assert len(json.dumps(payload, ensure_ascii=False)) <= module.RESULT_TOTAL_CHARS + 400

    # 方法作用：同一执行内成功结果命中缓存，重复查询不再访问向量库。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_caches_success_without_crossing_requests(self, monkeypatch) -> None:
        """同一执行内成功结果命中缓存，重复查询不再访问向量库。"""
        # Arrange
        import src.tools.knowledge_search as module

        search = AsyncMock(return_value=[_evidence()])
        monkeypatch.setattr(module, "search_knowledge", search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        first = await module.execute_knowledge_search(tools[0], {"query": "GMV"}, identity=_identity())
        second = await module.execute_knowledge_search(tools[0], {"query": "GMV"}, identity=_identity())

        # Assert
        assert first["status"] == "ok"
        assert second["evidence"] == first["evidence"]
        assert search.await_count == 1


class TestKnowledgeFiltersUnboundOnly:
    """覆盖功能 27.1.2 的未绑定知识过滤下推。"""

    # 方法作用：新增参数默认关闭，开启时下推精确空 datasource 且与非空值互斥。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_unbound_only_is_keyword_and_mutually_exclusive(self, monkeypatch) -> None:
        """新增参数默认关闭，开启时下推精确空 datasource 且与非空值互斥。"""
        # Arrange
        import src.knowledge.retrieval as retrieval_module
        from src.app_context import AppContext, use_app_context

        monkeypatch.setattr(retrieval_module, "get_current_tenant_id", lambda: 9)
        monkeypatch.setattr(retrieval_module, "get_current_user_id", lambda: 4)

        # Act
        with use_app_context(AppContext(SimpleNamespace(multi_tenant=True))):
            default_filters = retrieval_module.build_accessible_knowledge_filters()
            unbound_filters = retrieval_module.build_accessible_knowledge_filters(unbound_only=True)

            # Assert
            assert all("datasource" not in item for item in default_filters)
            assert [item["datasource"] for item in unbound_filters] == ["", "", ""]
            try:
                retrieval_module.build_accessible_knowledge_filters(
                    datasource="orders", unbound_only=True,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("unbound_only 与非空 datasource 必须互斥")

    # 方法作用：search_knowledge 把 unbound_only 透传到过滤条件。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_search_knowledge_downshifts_unbound_only(self, monkeypatch) -> None:
        """search_knowledge 把 unbound_only 透传到过滤条件。"""
        # Arrange
        import src.knowledge.retrieval as retrieval_module
        from src.memory.vector_store import VectorSearchResult

        captured: dict[str, object] = {}

        # 方法作用：覆盖 fake_filters 的测试场景
        # Args: 无
        # Returns: 见方法内返回
        def fake_filters(**kwargs):
            captured.update(kwargs)
            return [{"visibility": "system", "datasource": ""}]

        monkeypatch.setattr(retrieval_module, "build_accessible_knowledge_filters", fake_filters)
        store = SimpleNamespace(search=AsyncMock(return_value=[VectorSearchResult(
            id="doc-1", content="口径", metadata={"locator": {"page": 1}}, score=0.9,
        )]))

        # Act
        results = await retrieval_module.search_knowledge(store, "GMV", unbound_only=True)

        # Assert
        assert results[0].source_id == "doc-1"
        assert captured["unbound_only"] is True
