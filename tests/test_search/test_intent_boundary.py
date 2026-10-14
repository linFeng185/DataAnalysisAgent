"""否定授权边界测试：明确禁止联网与仅离线问法必须优先拒绝（spec/58 §3）。

覆盖功能 32.1：is_explicit_web_search 的否定优先判定、search_public_web 在否定
问法下的零身份读取与零 MCP 触达，以及 market_research 子图复用同一判定并以
needs_input 结束、不触发搜索服务与摘要模型。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

logger = logging.getLogger(__name__)

# 明确否定的问法集合：含外部独立验收暴露的两条真实问法与中英文常见否定形态。
_NEGATED_QUERIES = [
    "不要联网搜索，仅分析附件",
    "Do not search the web; use the attached report only.",
    "Don’t search the web; summarize the attachment.",
    "请勿联网检索，先基于附件回答",
    "别联网搜索，用内部知识回答",
    "不联网，仅根据已有资料总结",
    "不用联网，直接分析附件",
    "无需联网查询",
    "不要搜网页，只看附件",
    "不要使用联网搜索，直接回答",
    "不要进行联网查询，用附件内容",
    "不必使用搜索网页，总结附件",
    "仅离线分析这份报告",
    "offline only",
    "do not browse the web",
    "do not perform a web search",
    "do not use the internet",
    "without internet access",
    # 外部独立验收追加的常见禁止变体：否定词与联网/搜索类词不相邻或使用英文结构。
    "不能联网搜索，请只分析附件。",
    "别上网搜索，只用已有报告。",
    "禁止使用 web search，仅分析这份材料。",
    "You must not search the web; use only this report.",
    "No online search. Please analyze the attachment.",
    "Analyze the report without performing a web search.",
    # 同类结构的其它常见形态，锁定新增分支的作用边界。
    "不能联网，请分析附件",
    "不许联网搜索，直接基于附件回答",
    "禁止 web search，仅分析材料",
    "You cannot search the web; summarize the attached file.",
    "The assistant can not search the web; use the file.",
    "Analyze it without doing any web search.",
]

# 正常联网问法不得被否定标记误伤：附件、离线主题词与含「别/不」的普通词都不构成否定。
_POSITIVE_QUERIES = [
    "联网搜索",
    "联网调研一下竞品动态",
    "web search",
    "search the web",
    "请联网搜索并同时分析附件",
    "联网搜索 离线安装教程",
    "联网搜索 分别搜索各平台价格",
    "联网搜索有哪些别的搜索方式",
    "联网搜索 不同搜索方式的对比",
    # 新增否定词/结构的误伤保护：否定词后不接联网、搜索类词时仍是正常联网问法。
    "联网搜索 不能错过的最新行业报告",
    "联网搜索 特别搜索一下冷门数据集",
    "联网搜索 must not 的说法有哪些",
    "联网搜索 without 关键词 用法说明",
]


class TestNegatedIntentPriority:
    """覆盖功能 32.1：否定与离线意图优先于任何正向关键词。"""

    # 方法作用：验证明确否定问法一律不构成外送授权。
    # Args: self - 测试实例；query - 含否定或离线意图的问法。
    # Returns: 无返回值，误授权时由断言报告。
    @pytest.mark.parametrize("query", _NEGATED_QUERIES)
    def test_negated_intents_return_false(self, query: str) -> None:
        """用户明确禁止联网或要求仅离线时，必须优先返回 False。"""
        logger.debug("test_negated_intents_return_false 入口", extra={"query_chars": len(query)})
        try:
            # Arrange / Act
            from src.search.models import is_explicit_web_search

            # Assert
            assert is_explicit_web_search(query) is False
            logger.info("test_negated_intents_return_false 完成")
        except Exception as exc:
            logger.error("test_negated_intents_return_false 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证否定标记不会误伤正常联网问法。
    # Args: self - 测试实例；query - 正常联网问法。
    # Returns: 无返回值，误拒绝时由断言报告。
    @pytest.mark.parametrize("query", _POSITIVE_QUERIES)
    def test_positive_markers_still_authorized(self, query: str) -> None:
        """正常联网搜索/调研必须保持有效；附件与离线主题词不构成否定。"""
        logger.debug("test_positive_markers_still_authorized 入口", extra={"query_chars": len(query)})
        try:
            # Arrange / Act
            from src.search.models import is_explicit_web_search

            # Assert
            assert is_explicit_web_search(query) is True
            logger.info("test_positive_markers_still_authorized 完成")
        except Exception as exc:
            logger.error("test_positive_markers_still_authorized 异常", extra={"error": str(exc)}, exc_info=True)
            raise


# 方法作用：构造显式开启联网搜索的最小测试配置。
# Args: 无。
# Returns: 含联网搜索字段的 SimpleNamespace 配置。
def _enabled_settings() -> SimpleNamespace:
    return SimpleNamespace(
        web_search_enabled=True,
        web_search_mcp_tools="public_search__web_search",
        web_search_timeout_seconds=1.0,
        web_search_max_results=3,
        web_search_max_query_chars=256,
        mcp_config_path="unused-intent-boundary.yaml",
    )


# 方法作用：为服务模块注入记录型身份读取器与 MCP 管理器替身。
# Args: monkeypatch - pytest 补丁工具。
# Returns: (身份读取记录, 管理器工厂替身, 管理器替身) 三元组。
def _patch_service_boundary(monkeypatch):
    import src.search.service as service_module

    identity_reads: list[str] = []

    # 方法作用：记录一次用户编号读取并返回固定值。
    # Args: 无。
    # Returns: 固定 user_id=7。
    def _user_id() -> int:
        identity_reads.append("user_id")
        return 7

    # 方法作用：记录一次租户编号读取并返回固定值。
    # Args: 无。
    # Returns: 固定 tenant_id=3。
    def _tenant_id() -> int:
        identity_reads.append("tenant_id")
        return 3

    # 方法作用：记录一次角色读取并返回固定值。
    # Args: 无。
    # Returns: 固定 role=analyst。
    def _role() -> str:
        identity_reads.append("role")
        return "analyst"

    manager = SimpleNamespace(get_all_tools=MagicMock(return_value=[]))
    factory = MagicMock(return_value=manager)
    monkeypatch.setattr(service_module, "get_settings", lambda: _enabled_settings())
    monkeypatch.setattr(service_module, "get_current_user_id", _user_id)
    monkeypatch.setattr(service_module, "get_current_tenant_id", _tenant_id)
    monkeypatch.setattr(service_module, "get_current_role", _role)
    monkeypatch.setattr(service_module, "get_mcp_client_manager", factory)
    return identity_reads, factory, manager


class TestServiceNegatedIntentBoundary:
    """覆盖功能 32.1/32.2：否定问法在服务层零身份读取、零 MCP 触达。"""

    # 方法作用：验证否定问法在读取身份和枚举工具之前就被拒绝。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；query - 含否定意图的问法。
    # Returns: 无返回值，出现任何外送准备动作时由断言报告。
    @pytest.mark.parametrize("query", _NEGATED_QUERIES)
    async def test_negated_query_never_reaches_mcp(self, monkeypatch, query: str) -> None:
        """明确否定应固定返回 no_explicit_intent，不读身份、不碰管理器。"""
        logger.debug("test_negated_query_never_reaches_mcp 入口", extra={"query_chars": len(query)})
        try:
            # Arrange
            import src.search.service as service_module

            identity_reads, factory, manager = _patch_service_boundary(monkeypatch)

            # Act
            response = await service_module.search_public_web(query)

            # Assert
            assert response.success is False
            assert response.error_code == "no_explicit_intent"
            assert response.hits == []
            assert identity_reads == []
            factory.assert_not_called()
            manager.get_all_tools.assert_not_called()
            logger.info("test_negated_query_never_reaches_mcp 完成")
        except Exception as exc:
            logger.error("test_negated_query_never_reaches_mcp 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestMarketResearchReusesIntentJudgment:
    """覆盖功能 33.1 衔接边界：子图复用核心判定，否定问法不触发搜索与模型。"""

    # 方法作用：验证否定问法以 needs_input 结束且零搜索服务、零 MCP、零模型调用。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；query - 含否定意图的问法。
    # Returns: 无返回值，出现外送或模型调用时由断言报告。
    @pytest.mark.parametrize(
        "query",
        [
            "不要联网搜索，仅分析附件",
            "Do not search the web; use the attached report only.",
            "禁止使用 web search，仅分析这份材料。",
        ],
    )
    async def test_negated_query_ends_needs_input_without_model(self, monkeypatch, query: str) -> None:
        """子图门禁复用同一判定：拒绝结果直接结束，不调用搜索服务与摘要模型。"""
        logger.debug("test_negated_query_ends_needs_input_without_model 入口")
        try:
            # Arrange
            import src.graph.subgraphs.market_research as module
            import src.llm.client as llm_client
            import src.search.service as service_module
            from src.api.auth import use_identity_context
            from src.security.tenant_policy import RequestIdentity

            identity_reads, factory, _manager = _patch_service_boundary(monkeypatch)
            search_calls = AsyncMock(side_effect=AssertionError("否定问法不得调用搜索服务"))
            monkeypatch.setattr(service_module, "search_public_web", search_calls)
            monkeypatch.setattr(module, "_web_search_enabled", lambda: True)
            model_reads: list[str] = []

            # 方法作用：记录一次摘要模型可用性检查并返回可用。
            # Args: task - 任务标识。
            # Returns: 固定 True；任何调用都会在断言中暴露。
            def _available(task: str) -> bool:
                model_reads.append(str(task))
                return True

            # 方法作用：记录一次模型实例获取并返回空哨兵。
            # Args: *args/**kwargs - 工厂参数。
            # Returns: None；任何调用都会在断言中暴露。
            def _get_llm(*args, **kwargs):
                model_reads.append("get_task_llm")
                return None

            monkeypatch.setattr(llm_client, "is_task_llm_available", _available)
            monkeypatch.setattr(llm_client, "get_task_llm", _get_llm)
            state = {
                "user_query": query,
                "tenant_id": 3,
                "user_id": 7,
                "task_plan": {"capability": "market_research"},
            }

            # Act
            with use_identity_context(RequestIdentity(tenant_id=3, user_id=7, role="analyst")):
                result = await module.market_research_node(state)

            # Assert
            final = result["final_response"]
            assert final["status"] == "needs_input"
            assert final["success"] is True
            assert "联网" in final["analysis"]["summary"]
            assert search_calls.await_count == 0
            assert factory.call_count == 0
            assert identity_reads == []
            assert model_reads == []
            logger.info("test_negated_query_ends_needs_input_without_model 完成")
        except Exception as exc:
            logger.error("test_negated_query_ends_needs_input_without_model 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证子图门禁直接复用核心判定函数而不是另建规则。
    # Args: self - 测试实例。
    # Returns: 无返回值，判定分叉时由断言报告。
    def test_subgraph_consults_the_core_judgment_function(self) -> None:
        """市场研究子图加载的 is_explicit_web_search 必须是核心同一函数对象。"""
        logger.debug("test_subgraph_consults_the_core_judgment_function 入口")
        try:
            # Arrange
            import src.graph.subgraphs.market_research as module
            import src.search.models as models_module

            # Act
            service = module._load_search_service()

            # Assert
            assert service is not None
            assert service.is_explicit_web_search is models_module.is_explicit_web_search
            logger.info("test_subgraph_consults_the_core_judgment_function 完成")
        except Exception as exc:
            logger.error("test_subgraph_consults_the_core_judgment_function 异常", extra={"error": str(exc)}, exc_info=True)
            raise
