"""向量知识库的租户隔离渗透测试。

背景：Milvus 集合里混存多个租户的条目，向量库本身**不做任何内置隔离**——
它只看调用方传进来的 metadata 过滤条件。实测不带 tenant_id 过滤时，
租户 1 与租户 3 的表条目会被同时召回。

写入侧（schema_manager / long_term_store）是无条件写 tenant_id 的，因此读取侧
必须对称。历史上读取侧的租户过滤挂在 `multi_tenant` 开关上，而该开关在
config.py 里的默认值是 False（fail-open）：任何一次部署漏配，历史数据就会互相可见。

本文件锁住修复后的契约：
1. tenant / private 两档无条件带 tenant_id，与部署模式无关；
2. system 档保持跨租户共享，不受影响；
3. schema 语义筛表传给向量库的条件里必须含 tenant_id。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.knowledge.retrieval import build_accessible_knowledge_filters
from src.knowledge.schema_manager import get_schema_manager
from src.security.tenant_policy import TenantPolicy

_TENANT_ID = 3
_USER_ID = 9


# 方法作用：把租户/用户/角色上下文固定为普通租户成员。
# Args: monkeypatch - pytest 补丁夹具；multi_tenant - 多租户开关取值。
# Returns: 无返回值。
def _bind_context(monkeypatch: pytest.MonkeyPatch, *, multi_tenant: bool) -> None:
    policy = TenantPolicy(multi_tenant=multi_tenant)
    monkeypatch.setattr("src.knowledge.retrieval.get_tenant_policy", lambda: policy)
    monkeypatch.setattr("src.knowledge.retrieval.get_current_tenant_id", lambda: _TENANT_ID)
    monkeypatch.setattr("src.knowledge.retrieval.get_current_user_id", lambda: _USER_ID)
    monkeypatch.setattr("src.api.auth.get_current_role", lambda: "tenant_admin")


class TestAccessibleKnowledgeFilters:
    """覆盖知识检索的可见范围过滤。"""

    # 方法作用：三档可见范围的结构必须固定：system 共享、tenant/private 带租户。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_filters_cover_three_scopes_with_tenant_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_context(monkeypatch, multi_tenant=True)

        filters = build_accessible_knowledge_filters(datasource="mysql")

        assert [item["visibility"] for item in filters] == ["system", "tenant", "private"]
        assert "tenant_id" not in filters[0], "system 档是全租户共享，不能套租户过滤"
        assert filters[1]["tenant_id"] == _TENANT_ID
        assert filters[2]["tenant_id"] == _TENANT_ID
        assert filters[2]["owner_user_id"] == _USER_ID

    # 方法作用：回归 fail-open——关掉多租户开关（config 默认值）也必须带租户过滤。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    def test_tenant_bound_survives_disabled_isolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_context(monkeypatch, multi_tenant=False)

        filters = build_accessible_knowledge_filters(datasource="mysql")

        assert filters[1]["tenant_id"] == _TENANT_ID, "隔离开关关闭时不得退化成跨租户可见"
        assert filters[2]["tenant_id"] == _TENANT_ID


class TestSemanticTableFilterTenantBound:
    """覆盖 schema 语义筛表传给向量库的过滤条件。"""

    # 方法作用：筛表条件必须无条件含 tenant_id，与隔离开关无关。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_semantic_search_filters_carry_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        # 方法作用：捕获传给向量库的过滤条件，并返回空结果触发回退。
        # Args: query - 检索文本；top_k - 返回条数；filters - 元数据过滤条件。
        # Returns: 空列表。
        async def fake_search(query, top_k=5, filters=None):
            captured["filters"] = filters
            return []

        store = SimpleNamespace(search=fake_search)
        monkeypatch.setattr(
            "src.memory.vector_store.get_vector_store",
            AsyncMock(return_value=store),
        )
        # 隔离开关关闭：旧实现下这里会丢掉 tenant_id。
        monkeypatch.setattr(
            "src.app_context.get_tenant_policy",
            lambda: TenantPolicy(multi_tenant=False),
        )
        manager = get_schema_manager()
        tables = [SimpleNamespace(name="retail_orders", relations=[])]

        await manager._semantic_search_tables("订单净销售额", "mysql", tables, tenant_id=_TENANT_ID)

        assert captured["filters"]["tenant_id"] == _TENANT_ID
        assert captured["filters"]["datasource"] == "mysql"
        assert captured["filters"]["category"] == "table"
