"""覆盖显式指标引用的授权、生效版本和知识来源传递。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.knowledge.metric_catalog import resolve_metric_references
from src.security.tenant_policy import RequestIdentity


class TestMetricReferences:
    """覆盖 11.1.13：只读取已批准版本，不静默忽略错误引用。"""

    # 方法作用：验证固定版本、数据源和显式身份传递给目录。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_reference_pins_version_and_datasource(self) -> None:
        """知识来源包含公式快照和版本，保障历史可追溯。"""
        store = AsyncMock()
        store.effective.return_value = {"code": "gmv", "version": 2, "definition": {
            "name": "支付金额", "expression": "SUM(amount)", "description": "仅支付订单", "unit": "元",
        }}
        identity = RequestIdentity(tenant_id=3, user_id=7, role="analyst")
        text, sources = await resolve_metric_references("分析 @metric(gmv@2)", "sales", identity, catalog=store)
        assert "SUM(amount)" in text
        assert sources[0]["locator"]["version"] == 2
        store.effective.assert_awaited_once_with(identity, "gmv", version=2, datasource="sales")

    # 方法作用：验证无引用时不连接存储，重复引用只读取一次。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_no_reference_has_no_database_dependency(self) -> None:
        """旧查询行为不受新增目录影响。"""
        store = AsyncMock()
        identity = RequestIdentity(tenant_id=3, user_id=7, role="analyst")
        assert await resolve_metric_references("销售额趋势", "sales", identity, catalog=store) == ("", [])
        store.effective.assert_not_awaited()

    # 方法作用：验证不存在和格式错误的引用均不降级到无口径查询。
    # Args: self - 实例；query - 错误引用。
    # Returns: 无返回值。
    @pytest.mark.parametrize("query", ["@metric(missing)", "@metric(非法编码)", "@metric(gmv@0)"])
    async def test_invalid_reference_fails_closed(self, query) -> None:
        """指定口径不可用时必须报错，不能让模型自由猜测。"""
        store = AsyncMock()
        store.effective.side_effect = LookupError("尚未批准")
        identity = RequestIdentity(tenant_id=3, user_id=7, role="analyst")
        with pytest.raises((LookupError, ValueError)):
            await resolve_metric_references(query, "sales", identity, catalog=store)

    # 方法作用：验证 Schema 节点把批准口径及版本来源写入 SQL 生成上下文。
    # Args: self - 实例；monkeypatch - 边界补丁。
    # Returns: 无返回值。
    async def test_schema_node_preserves_reference_evidence(self, monkeypatch) -> None:
        """图节点不依赖 ContextVar 猜身份，使用明确请求租户与用户。"""
        import src.datasource.registry as registry_module
        import src.graph.nodes.retrieve_schema as schema_module
        import src.graph.skill_activation as skill_module
        import src.knowledge.metric_catalog as catalog_module

        monkeypatch.setattr(registry_module, "get_registry", lambda: SimpleNamespace(resolve_or_none=AsyncMock(return_value=SimpleNamespace(dialect="postgres"))))
        monkeypatch.setattr(schema_module, "_load_knowledge_context", AsyncMock(return_value="已有知识"))
        monkeypatch.setattr(schema_module, "_load_enum_dictionary", AsyncMock(return_value={}))
        monkeypatch.setattr(skill_module, "activate_skills", lambda *args: {})
        reference = AsyncMock(return_value=("已批准指标 SUM(amount)", [{"source_id": "metric:3:gmv:v2", "category": "business_rule"}]))
        monkeypatch.setattr(catalog_module, "resolve_metric_references", reference)
        result = await schema_module.retrieve_schema_node({
            "user_query": "分析 @metric(gmv@2)", "datasource": "sales",
            "tenant_id": 3, "user_id": 7, "user_role": "analyst",
            "resolved_schema": SimpleNamespace(tables=[], business_rules=[], sql_templates=[]),
        })
        assert "SUM(amount)" in result["long_term_memories_text"]
        assert result["activated_knowledge"][0]["source_id"] == "metric:3:gmv:v2"
        assert reference.await_args.args[2].tenant_id == 3
