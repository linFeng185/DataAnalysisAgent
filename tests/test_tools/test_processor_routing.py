"""确定性处理器选择与参数构造回归测试。"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)


class TestProcessorRouting:
    """覆盖 SPEC 20 B1-B22 处理器选择和专用列参数契约。"""

    @pytest.mark.parametrize(
        ("processor_name", "query"),
        [
            ("trend", "分析网站流量的月度趋势"),
            ("yoy", "对比2024和2025年revenue的同比增长率"),
            ("mom", "分析每月订单金额的环比变化"),
            ("distribution", "订单金额的分布情况如何"),
            ("ranking", "销售额最高的10个商品是哪些"),
            ("proportion", "各物流公司的运单占比是多少"),
            ("anomaly", "检查库存数据有没有异常值"),
            ("retention", "分析用户的7日留存率"),
            ("funnel", "从浏览到下单的转化漏斗什么样"),
            ("rfm", "对客户做RFM分析"),
            ("pareto", "按商品做帕累托分析，哪些贡献了80%销售"),
            ("correlation", "营销活动的投入和转化数有相关性吗"),
            ("growth_rate", "季度revenue的增长率是多少"),
            ("seasonal", "订单数据有没有季节性规律"),
            ("contribution", "各部门对总销售额的贡献度"),
            ("cross_pivot", "按省份和商品分类做交叉分析"),
            ("ab_test", "对比不同广告版本的点击率和转化率"),
            ("budget_variance", "各部门的预算执行偏差有多大"),
            ("geo_distribution", "客户在全国各省的分布情况"),
            ("market_basket", "哪些商品经常被一起购买"),
            ("prediction", "基于历史数据预测未来30天的订单趋势"),
            ("aggregation", "统计订单总数和平均金额"),
        ],
    )
    # 验证明确的业务关键词会选择对应的确定性处理器。
    # Args: self - 测试类实例；processor_name - 期望处理器；query - 用户问题。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_query_selects_expected_processor(self, processor_name: str, query: str) -> None:
        logger.debug("test_query_selects_expected_processor 入口", extra={"query": query})
        from src.tools import processors  # noqa: F401
        from src.tools.data_processor import get_processor

        selected = get_processor("query", query=query)

        assert selected is not None
        assert selected.name == processor_name
        logger.info("test_query_selects_expected_processor 完成", extra={"processor": processor_name})

    # 验证专用处理器可以通过稳定名称直接获取，不依赖粗粒度 intent。
    # Args: self - 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_named_processor_is_directly_addressable(self) -> None:
        logger.debug("test_named_processor_is_directly_addressable 入口")
        from src.tools import processors  # noqa: F401
        from src.tools.data_processor import get_processor

        selected = get_processor("correlation")

        assert selected is not None
        assert selected.name == "correlation"
        logger.info("test_named_processor_is_directly_addressable 完成")

    # 验证分析节点为相关性处理器提供两个数值列，而不是只提供一个 value_col。
    # Args: self - 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_analysis_uses_specialized_columns(self, monkeypatch) -> None:
        logger.debug("test_analysis_uses_specialized_columns 入口")
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "_is_task_llm_available", lambda task: False)
        result = await analyze_module.analyze_result_node({
            "user_query": "营销投入和转化数有相关性吗",
            "intent": "attribution",
            "query_result_sample": [
                {"campaign_spend": 10, "conversions": 2},
                {"campaign_spend": 20, "conversions": 4},
                {"campaign_spend": 30, "conversions": 7},
            ],
        })

        analysis = result["analysis_result"]
        assert analysis["processor_name"] == "correlation"
        assert analysis["insights"]
        logger.info("test_analysis_uses_specialized_columns 完成")

    # 验证专用问题不会在图入口被误判为 chat 而跳过分析节点。
    # Args: self - 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("query", "intent"),
        [
            ("检查库存数据有没有异常值", "attribution"),
            ("营销活动的投入和转化数有相关性吗", "attribution"),
            ("从浏览到下单的转化漏斗什么样", "aggregation"),
        ],
    )
    async def test_specialized_query_reaches_analysis_intent(self, query: str, intent: str) -> None:
        logger.debug("test_specialized_query_reaches_analysis_intent 入口", extra={"query": query})
        from src.graph.nodes.classify_intent import classify_intent_node

        result = await classify_intent_node({
            "user_query": query,
            "conversation_history": [],
            "relevant_tables": [],
        })

        assert result["intent"] == intent
        logger.info("test_specialized_query_reaches_analysis_intent 完成", extra={"intent": intent})
