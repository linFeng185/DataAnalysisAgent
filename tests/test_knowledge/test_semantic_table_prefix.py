"""SchemaManager 语义筛表的条目前缀契约测试。

覆盖缺陷：_semantic_search_tables 的过滤条件用 effective_tenant_id
（显式 tenant_id 优先），而 id 前缀却由 _scoped_entry_id 默认读
_current_tenant_id()。两者不一致时，已经检索到的候选会被整批丢弃，
表现为「语义筛出 0 张表」，上游据此「回退到全部表」把整库塞进 Prompt。

契约要点：
1. 前缀必须与过滤条件用同一个租户；
2. 不能把别的租户的同名数据源条目算进来。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.knowledge.schema_manager import SchemaManager, get_schema_manager
from src.memory.vector_store import VectorSearchResult

_TABLES = ["retail_orders", "retail_returns", "retail_products"]


# 方法作用：构造一个只返回固定候选的假向量库。
# Args: candidates - VectorSearchResult 列表。
# Returns: 具备 search 协程的假 store。
def _fake_store(candidates: list[VectorSearchResult]) -> SimpleNamespace:
    store = SimpleNamespace()
    store.search = AsyncMock(return_value=candidates)
    return store


# 方法作用：把租户隔离策略固定为启用，使条目前缀带租户段。
# Args: monkeypatch - pytest 补丁夹具。
# Returns: 无返回值。
def _enable_tenant_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.app_context.get_tenant_policy",
        lambda: SimpleNamespace(knowledge_isolation_enabled=True),
    )


# 方法作用：构造只含表名与关系的轻量表对象。
# Args: 无。
# Returns: 表对象列表。
def _tables() -> list[SimpleNamespace]:
    return [SimpleNamespace(name=name, relations=[]) for name in _TABLES]


class TestSemanticTablePrefix:
    """覆盖语义筛表的条目前缀与租户一致性。"""

    # 方法作用：显式 tenant_id 检索时，带同租户前缀的候选必须被采纳。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_explicit_tenant_prefix_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tenant_isolation(monkeypatch)
        candidates = [
            VectorSearchResult(
                id="table:7:mysql.retail_orders",
                content="retail_orders",
                metadata={"table_name": "retail_orders"},
                score=0.2,
            )
        ]
        monkeypatch.setattr(
            "src.memory.vector_store.get_vector_store",
            AsyncMock(return_value=_fake_store(candidates)),
        )
        manager = get_schema_manager()

        matched = await manager._semantic_search_tables(
            "订单净销售额", "mysql", _tables(), tenant_id=7,
        )

        assert matched == {"retail_orders"}

    # 方法作用：别的租户的同名数据源条目不得被采纳。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_other_tenant_candidate_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tenant_isolation(monkeypatch)
        candidates = [
            VectorSearchResult(
                id="table:9:mysql.retail_orders",
                content="retail_orders",
                metadata={"table_name": "retail_orders"},
                score=0.2,
            )
        ]
        monkeypatch.setattr(
            "src.memory.vector_store.get_vector_store",
            AsyncMock(return_value=_fake_store(candidates)),
        )
        manager = get_schema_manager()

        matched = await manager._semantic_search_tables(
            "订单净销售额", "mysql", _tables(), tenant_id=7,
        )

        assert matched == set()

    # 方法作用：检索为空时回退到全部表，这是既有语义，不能被本次改动破坏。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_empty_candidates_fall_back_to_all_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tenant_isolation(monkeypatch)
        monkeypatch.setattr(
            "src.memory.vector_store.get_vector_store",
            AsyncMock(return_value=_fake_store([])),
        )
        manager = get_schema_manager()

        matched = await manager._semantic_search_tables(
            "订单净销售额", "mysql", _tables(), tenant_id=7,
        )

        assert matched == set(_TABLES)


class TestLexicalTableHints:
    """覆盖词面通道：英文 embedding 对中文 query 排序失准时补回相关表。

    回归 2026-09-13：marketing_attribution 单元在 ch 上失败，模型报
    「白名单中不存在广告归因事实表 insight_order_attribution」——该表确实存在，
    但语义 top-20 全是 weather / tourism / consumer_trends 类表，它连前 50 都进不去
    （embedding 是英文模型 all-MiniLM-L6-v2，中文 query 的排序接近噪声）。
    """

    # 方法作用：中文业务词必须能把语义漏掉的归因表补回候选。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_chinese_business_word_recovers_attribution_table(self) -> None:
        tables = [
            SimpleNamespace(name=name)
            for name in (
                "insight_consumer_trends",
                "insight_tourism_flows",
                "insight_market_indices",
                "insight_order_attribution",
            )
        ]

        hits = SchemaManager._lexical_hit_tables("广告归因收入与订单净销售额的对比", tables)

        assert "insight_order_attribution" in hits
        assert "insight_consumer_trends" not in hits

    # 方法作用：宽泛片段不得吃光配额，挤掉同样命中但数量少的片段。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_broad_fragment_does_not_starve_narrow_one(self) -> None:
        tables = [
            SimpleNamespace(name=name)
            for name in (
                "supply_supplier_audits",
                "supply_supplier_bids",
                "supply_supplier_scores",
                "supply_suppliers",
                "supply_shipments",
                "supply_shipment_events",
            )
        ]

        hits = SchemaManager._lexical_hit_tables("配送费用与采购成本", tables)

        assert "supply_shipments" in hits

    # 方法作用：问题不含业务词时通道静默跳过，不凭空补表。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_no_business_word_returns_empty(self) -> None:
        tables = [SimpleNamespace(name="retail_orders")]

        assert SchemaManager._lexical_hit_tables("zzz_unmatched", tables) == set()

    # 方法作用：验证未显式写订单的销售问题仍保留承载销售金额的订单事实表。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_sales_keeps_order_fact_without_order_keyword(self) -> None:
        """真实促销深挖问题曾因漏掉 retail_orders 被错误判成没有销售数据。"""
        # Arrange
        tables = [SimpleNamespace(name=name) for name in ("retail_orders", "retail_promotions", "retail_customer_notes")]

        # Act
        hits = SchemaManager._lexical_hit_tables("按年月、地区统计促销活动期间的销售提升幅度与促销后回落幅度", tables)

        # Assert
        assert "retail_orders" in hits
        assert "retail_promotions" in hits
        assert "retail_customer_notes" not in hits
        assert SchemaManager._lexical_hit_tables("销售提升", []) == set()

    # 方法作用：验证会员价值分层类问题能把 RFM / LTV / 流失 / 权益发放核销主表全部召回。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_member_value_tables_recalled(self) -> None:
        """实机回归 2026-09-19：run_id=cc74fa66-a0f1-4242-8f0f-e1b1d09bcdea 的 customer_member 单元失败。

        原始问题：「按 RFM 分层统计客户规模与消费贡献，分析流失信号分布、生命周期
        价值分层，以及会员权益的发放与核销规模对比，时间范围为去年。」
        MySQL Schema 中 retail_rfm_scores / retail_lifetime_values /
        retail_churn_signals / retail_benefit_grants 全都存在，但词面通道对 RFM、
        流失、生命周期价值、权益发放四个概念没有映射，只靠「核销」偶然捞回
        retail_benefit_redemptions，模型据残缺白名单判定「不存在 RFM / 流失 /
        生命周期价值事实表」而拒绝生成 SQL。
        """
        # Arrange：表名取自 retail 库真实清单；benefit_* 干扰项用于验证
        # _LEXICAL_PER_FRAGMENT=3 的配额不会把 grants / redemptions 挤出去。
        tables = [
            SimpleNamespace(name=name)
            for name in (
                "retail_rfm_scores",
                "retail_lifetime_values",
                "retail_churn_signals",
                "retail_benefit_grants",
                "retail_benefit_redemptions",
                "retail_benefit_rules",
                "retail_benefit_catalog",
                "retail_partner_benefits",
                "retail_benefit_complaints",
                "retail_contract_releases",
            )
        ]
        query = (
            "按 RFM 分层统计客户规模与消费贡献，分析流失信号分布、生命周期价值分层，"
            "以及会员权益的发放与核销规模对比，时间范围为去年。"
        )

        # Act
        hits = SchemaManager._lexical_hit_tables(query, tables)

        # Assert
        assert {
            "retail_rfm_scores",
            "retail_lifetime_values",
            "retail_churn_signals",
            "retail_benefit_grants",
            "retail_benefit_redemptions",
        } <= hits
        assert "retail_contract_releases" not in hits

    # 方法作用：验证定价/折扣/销量弹性/毛利告警/降价计划类问题能确定性召回全部核心表。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_pricing_markdown_tables_recalled(self) -> None:
        """实机回归 2026-09-19：run_id=517e0f12-e56a-4f83-82e4-4993aace1d0e 的 price_promotion 单元 QUERY_FAILED。

        原始问题：「对比不同定价区域的折扣深度与销量弹性，识别毛利告警商品，
        并评估降价计划对清库存的实际贡献，时间范围为去年。」
        MySQL Schema 中 retail_order_items / retail_products / retail_price_zones /
        retail_price_elasticities / retail_margin_alerts / retail_markdown_plans /
        retail_markdown_items 七张表全部真实存在（日志已核实），但词面通道只由
        「区域」「库存」产出 region / inventory / stock 三个片段，七张核心表一张
        都没进白名单，模型据此回答「这些表不在本数据源白名单」并返回空 SQL。
        """
        # Arrange：表名取自 retail 库真实清单；product_* / inventory_* / region_*
        # 为干扰项，用于验证 _LEXICAL_PER_FRAGMENT=3 的配额不会把主表挤出去
        # （retail_product_* 在字母序上排在 retail_products 之前）。
        tables = [
            SimpleNamespace(name=name)
            for name in (
                # 七张核心表
                "retail_order_items",
                "retail_products",
                "retail_price_zones",
                "retail_price_elasticities",
                "retail_margin_alerts",
                "retail_markdown_plans",
                "retail_markdown_items",
                # 真实库里同前缀的干扰表
                "retail_orders",
                "retail_inventory",
                "retail_stock_reviews",
                "retail_region_targets",
                "retail_product_categories",
                "retail_product_reviews",
                "retail_product_suppliers",
                "retail_markdown_events",
                "retail_customer_notes",
                "retail_supplier_contracts",
            )
        ]
        query = (
            "对比不同定价区域的折扣深度与销量弹性，识别毛利告警商品，"
            "并评估降价计划对清库存的实际贡献，时间范围为去年。"
        )

        # Act
        hits = SchemaManager._lexical_hit_tables(query, tables)

        # Assert
        assert {
            "retail_order_items",
            "retail_products",
            "retail_price_zones",
            "retail_price_elasticities",
            "retail_margin_alerts",
            "retail_markdown_plans",
            "retail_markdown_items",
        } <= hits
        assert "retail_customer_notes" not in hits
        assert "retail_supplier_contracts" not in hits

    # 方法作用：验证问题里直接写英文缩写 RFM / LTV 时同样能召回对应主表。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    def test_rfm_ltv_abbreviation_recalled(self) -> None:
        """实机回归：用户常用英文缩写提问，缩写与中文词必须都能命中同一批表。"""
        # Arrange
        tables = [
            SimpleNamespace(name=name)
            for name in ("retail_rfm_scores", "retail_lifetime_values", "retail_orders")
        ]

        # Act
        hits = SchemaManager._lexical_hit_tables("用 RFM 和 LTV 给客户分层", tables)

        # Assert
        assert "retail_rfm_scores" in hits
        assert "retail_lifetime_values" in hits
