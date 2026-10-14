"""意图授权边界测试：只认请求位置的明确执行语法，否定、陈述与仅提及问法必须拒绝（spec/58 §3）。

覆盖功能 32.1：is_explicit_web_search 的有限执行句式识别（可含有限礼貌前缀）、
否定优先判定、陈述与咨询类问法默认失败关闭，search_public_web 在拒绝问法下的
零身份读取与零 MCP 触达，以及 market_research 子图复用同一判定并以
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
    # 明确执行请求的受保护形态：任务要求的合法问法必须保持授权。
    "联网搜索某主题",
    "请帮我联网搜索某主题",
    "Please search the web for X",
    "Can you search the web for X",
    "Please do a web search for X",
    "web search for X",
    # 名词式裸命令的保留契约：bare 命令或空白 + for 引入主题仍属执行请求。
    "online search",
    "online search for public datasets",
    # 搜索主题本身含 what/how/关闭/解释等词：主题词不改变执行请求性质。
    "Search the web for what quantum computing means",
    "联网搜索如何关闭某产品",
    "联网搜索 什么是量子计算",
    "联网搜索 如何关闭 iPhone 后台刷新",
    "联网搜索 how to disable a browser extension",
    "search the web for how to disable Windows updates",
    "联网搜索 解释器性能对比",
    "联网搜索 支持离线部署的向量数据库",
    "联网搜索 如何使用 pandas 分组聚合",
    "联网搜索 怎么用 Python 读取 Excel",
    # 外部独立验收追加：明确执行后的合法主题即使含 web search/解释/支持/引用 等词也必须授权。
    "Please search the web for an explanation of web search.",
    "Search the web for how to enable web search in a browser.",
    "Can you search the web for what quantum computing means?",
    "联网搜索什么是网页搜索",
    "请帮我联网搜索“联网搜索”这个术语的定义",
    "联网搜索 解释 web search 的含义",
    "联网搜索 支持 web search 的浏览器插件有哪些",
    "联网搜索 引用规范 什么是合理引用",
]

# 仅提及或咨询联网搜索的问法集合：引用标记本身、询问能力/含义/设置或要求解释，
# 都不是用户下达的搜索动作，必须失败关闭（同类句式的确定性规则，不逐条枚举句子）。
_MENTION_ONLY_QUERIES = [
    # 主代理外部独立验收暴露的六条真实问法。
    "请解释“联网搜索”是什么功能。",
    "这个系统支持联网搜索吗？",
    "文档里写着“联网搜索”，这是什么意思？",
    "What does web search mean?",
    "How do I disable web search?",
    "Explain the difference between web search and database search.",
    # 同类措辞：引号引用标记本身（讨论对象而非执行动作）。
    "请说明一下「搜索网页」这个能力。",
    '文档中的 "web search" 指的是什么？',
    "`联网调研` 是什么意思？",
    # 同类措辞：能力咨询与介绍请求。
    "这个平台具备网上搜索功能吗？",
    "系统是否支持联网检索？",
    "产品有没有 online search 能力？",
    "请介绍一下联网检索的实现原理。",
    # 同类措辞：定义与解释问法。
    "什么是联网搜索？",
    "解释一下 search the web 的原理。",
    "What is online search?",
    # 同类措辞：设置与开关咨询。
    "如何关闭联网搜索？",
    "怎么开启联网搜索功能？",
    "Where can I turn off web search?",
    "联网搜索如何关闭？",
    # 同类措辞：使用方式咨询。
    "怎么使用联网搜索功能？",
    "联网搜索怎么用？",
    # 同类措辞：与其它搜索方式作对比。
    "联网搜索和数据库查询有什么区别？",
]

# 陈述与咨询问法集合：没有在请求位置下达搜索动作的「提及型」表述，
# 无论句中何处出现关键词都必须默认失败关闭（同类句式的确定性规则，不逐条枚举句子）。
_STATEMENT_QUERIES = [
    # 主代理外部独立验收暴露的能力问句与陈述。
    "Is web search available?",
    "Does this app offer web search?",
    "Tell me about web search.",
    "The documentation mentions web search.",
    "这份说明包含联网搜索功能。",
    "我想了解联网搜索的使用限制。",
    "我们正在讨论网上查询这个术语。",
    # 同类措辞：英文能力咨询、介绍请求、陈述句与定义句，搜索标记只是被谈论的对象。
    "Please tell me about web search.",
    "Can you explain what web search means?",
    "web search is available?",
    "web search means finding public information.",
    "online search refers to finding public information.",
    "web search denotes an online capability.",
    # 同类措辞：中文陈述句与能力咨询，标记出现在句中任意位置。
    "系统具备网上搜索能力吗？",
    "说明书里提到了联网搜索。",
    "我们在讨论联网检索的适用范围。",
    "有 联网搜索 吗？",
    # 同类措辞：孤立立场单字（否定、禁止、谈论、人称）不能把残词读成执行授权。
    "不 联网搜索",
    "禁 联网搜索",
    "谈 联网搜索",
    "别 联网检索",
    "说 网上搜索",
    "我 联网搜索",
    # 复审退回：任意未识别单字前缀不构成授权（未知默认拒绝），残词噪声兼容已删除。
    "空 联网搜索",
    "空  联网检索  空白",
    "嗯 联网搜索",
    "喂 联网检索",
    # 复审退回：英文名词式 web search / online search 之后的未列出谓语同样是陈述，
    # 不能靠不断增长的系动词黑名单兜底；名词式只认裸命令或空白 + for 引入主题。
    "web search returns public information.",
    "online search has privacy risks.",
    "web search gives quick answers.",
    "web search 公开资料",
    # 同类措辞：问法以搜索标记开头，但整句是在询问该能力本身。
    "联网搜索是什么意思？",
    "联网搜索有什么限制？",
    "联网搜索呢？",
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


class TestMentionOnlyIntentNotAuthorized:
    """覆盖功能 32.1：仅提及、引用或咨询联网搜索的问法不构成外送授权。"""

    # 方法作用：验证引用、能力咨询、含义解释与设置开关问法一律返回 False。
    # Args: self - 测试实例；query - 仅讨论联网搜索而非请求执行的问法。
    # Returns: 无返回值，误授权时由断言报告。
    @pytest.mark.parametrize("query", _MENTION_ONLY_QUERIES)
    def test_mention_only_queries_return_false(self, query: str) -> None:
        """把联网搜索当作讨论对象的问法必须失败关闭，不能凭正向关键词放行。"""
        logger.debug("test_mention_only_queries_return_false 入口", extra={"query_chars": len(query)})
        try:
            # Arrange / Act
            from src.search.models import is_explicit_web_search

            # Assert
            assert is_explicit_web_search(query) is False
            logger.info("test_mention_only_queries_return_false 完成")
        except Exception as exc:
            logger.error("test_mention_only_queries_return_false 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestStatementIntentNotAuthorized:
    """覆盖功能 32.1：未下达执行动作的陈述与能力问句必须默认失败关闭。"""

    # 方法作用：验证陈述、能力问句与自指问法一律返回 False。
    # Args: self - 测试实例；query - 未在请求位置下达搜索动作的表述。
    # Returns: 无返回值，误授权时由断言报告。
    @pytest.mark.parametrize("query", _STATEMENT_QUERIES)
    def test_statement_queries_return_false(self, query: str) -> None:
        """关键词出现在陈述或问句任意位置都不构成授权，只有请求位置的执行语法才授权。"""
        # Arrange
        from src.search.models import is_explicit_web_search

        # Act
        result = is_explicit_web_search(query)

        # Assert
        assert result is False, f"未下达执行动作的表述被误判为授权：{query!r}"


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


class TestServiceMentionOnlyBoundary:
    """覆盖功能 32.1/32.2：仅咨询问法在服务层零身份读取、零 MCP 触达。"""

    # 方法作用：验证仅提及/咨询问法在读取身份和枚举工具之前就被拒绝。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；query - 仅讨论联网搜索的问法。
    # Returns: 无返回值，出现任何外送准备动作时由断言报告。
    @pytest.mark.parametrize(
        "query",
        [
            "请解释“联网搜索”是什么功能。",
            "这个系统支持联网搜索吗？",
            "文档里写着“联网搜索”，这是什么意思？",
            "What does web search mean?",
            "How do I disable web search?",
            "Explain the difference between web search and database search.",
        ],
    )
    async def test_mention_only_query_never_reaches_mcp(self, monkeypatch, query: str) -> None:
        """仅咨询联网搜索应固定返回 no_explicit_intent，不读身份、不碰管理器。"""
        logger.debug("test_mention_only_query_never_reaches_mcp 入口", extra={"query_chars": len(query)})
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
            logger.info("test_mention_only_query_never_reaches_mcp 完成")
        except Exception as exc:
            logger.error("test_mention_only_query_never_reaches_mcp 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestServiceStatementBoundary:
    """覆盖功能 32.1/32.2：陈述与能力问句在服务层零身份读取、零 MCP 触达。"""

    # 方法作用：验证陈述类问法在读取身份和枚举工具之前就被拒绝。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；query - 未下达执行动作的表述。
    # Returns: 无返回值，出现任何外送准备动作时由断言报告。
    @pytest.mark.parametrize("query", _STATEMENT_QUERIES)
    async def test_statement_query_never_reaches_mcp(self, monkeypatch, query: str) -> None:
        """未识别为执行语法的表述固定返回 no_explicit_intent，不读身份、不碰管理器。"""
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

    # 方法作用：验证仅提及/咨询问法以 needs_input 结束且零搜索服务、零 MCP、零模型调用。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；query - 仅讨论联网搜索的问法。
    # Returns: 无返回值，出现外送或模型调用时由断言报告。
    @pytest.mark.parametrize(
        "query",
        [
            "请解释“联网搜索”是什么功能。",
            "这个系统支持联网搜索吗？",
            "What does web search mean?",
            "How do I disable web search?",
        ],
    )
    async def test_mention_only_query_ends_needs_input_without_model(self, monkeypatch, query: str) -> None:
        """子图门禁复用同一判定：仅咨询联网搜索的问法直接结束，不外送、不调用模型。"""
        logger.debug("test_mention_only_query_ends_needs_input_without_model 入口")
        try:
            # Arrange
            import src.graph.subgraphs.market_research as module
            import src.llm.client as llm_client
            import src.search.service as service_module
            from src.api.auth import use_identity_context
            from src.security.tenant_policy import RequestIdentity

            identity_reads, factory, _manager = _patch_service_boundary(monkeypatch)
            search_calls = AsyncMock(side_effect=AssertionError("仅咨询问法不得调用搜索服务"))
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
            logger.info("test_mention_only_query_ends_needs_input_without_model 完成")
        except Exception as exc:
            logger.error("test_mention_only_query_ends_needs_input_without_model 异常", extra={"error": str(exc)}, exc_info=True)
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


class TestMarketResearchStatementBoundary:
    """覆盖功能 33.1 衔接边界：子图对陈述与自指问法同样以 needs_input 结束。"""

    # 方法作用：验证陈述类问法以 needs_input 结束且零搜索服务、零 MCP、零模型调用。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；query - 未下达执行动作的表述。
    # Returns: 无返回值，出现外送或模型调用时由断言报告。
    @pytest.mark.parametrize(
        "query",
        [
            "Is web search available?",
            "我想了解联网搜索的使用限制。",
            "联网搜索是什么意思？",
        ],
    )
    async def test_statement_query_ends_needs_input_without_model(self, monkeypatch, query: str) -> None:
        """子图门禁复用同一判定：未识别为执行语法的表述直接结束，不外送、不调用模型。"""
        # Arrange
        import src.graph.subgraphs.market_research as module
        import src.llm.client as llm_client
        import src.search.service as service_module
        from src.api.auth import use_identity_context
        from src.security.tenant_policy import RequestIdentity

        identity_reads, factory, _manager = _patch_service_boundary(monkeypatch)
        search_calls = AsyncMock(side_effect=AssertionError("陈述问法不得调用搜索服务"))
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
