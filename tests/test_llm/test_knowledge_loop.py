"""统一有界知识检索工具循环测试，覆盖功能 27.1.3。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from src.knowledge.asset_models import Evidence
from src.llm.invocation import PreparedInvocation
from src.security.tenant_policy import RequestIdentity


# 方法作用：构造一条知识证据，供工具检索结果使用。
# Args: source_id - 证据标识；content - 正文。
# Returns: 示例 Evidence。
def _evidence(source_id: str = "doc-1", content: str = "GMV 口径为已支付订单金额") -> Evidence:
    return Evidence(
        content=content,
        source_id=source_id,
        version="v1",
        locator={"page": 1},
        scores={"relevance": 0.9},
        metadata={"category": "metric", "source": "manual_doc"},
    )


# 方法作用：构造测试用的可信请求身份。
# Args: user_id - 用户 ID。
# Returns: RequestIdentity。
def _identity(user_id: int = 7) -> RequestIdentity:
    return RequestIdentity(tenant_id=2, user_id=user_id, role="analyst", username="tester")


# 方法作用：构造携带工具调用参数的流式分片序列，用于验证 tool_call_chunks 合并。
# Args: name - 工具名；call_id - 调用 ID；args_parts - 按顺序拼接的参数文本分片。
# Returns: AIMessageChunk 列表。
def _tool_round_chunks(name: str, call_id: str, args_parts: list[str]) -> list[AIMessageChunk]:
    chunks = []
    for index, part in enumerate(args_parts):
        chunks.append(AIMessageChunk(content="", tool_call_chunks=[{
            "name": name if index == 0 else None,
            "args": part,
            "id": call_id if index == 0 else None,
            "index": 0,
        }]))
    return chunks


class _BoundModel:
    """记录工具绑定调用的模型包装，用于验证回合与配置传递。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, inner: "_FakeToolModel", tools: list, config: dict | None = None) -> None:
        self._inner = inner
        self.tools = list(tools)
        self.config = dict(config or {})

    # 方法作用：模拟 Runnable 配置包装，返回自身以保留调用记录
    # Args: config - 运行配置
    # Returns: 包装后的模型对象
    def with_config(self, config):
        return _BoundModel(self._inner, self.tools, {**self.config, **dict(config or {})})

    # 方法作用：模拟模型非流式调用并记录消息与配置
    # Args: messages - 消息序列；config - 运行配置
    # Returns: 脚本中的下一条响应
    async def ainvoke(self, messages, config=None):
        self._inner._active_tools = self.tools
        try:
            return await self._inner.ainvoke(messages, config)
        finally:
            self._inner._active_tools = []

    # 方法作用：模拟模型流式调用并逐块产出脚本分片
    # Args: messages - 消息序列；config - 运行配置
    # Returns: 分片异步生成器
    async def astream(self, messages, config=None):
        self._inner._active_tools = self.tools
        try:
            async for chunk in self._inner.astream(messages, config):
                yield chunk
        finally:
            self._inner._active_tools = []


class _FakeToolModel:
    """按脚本返回响应并记录调用的测试模型，支持 bind_tools 与 with_config。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, responses: list, *, supports_tools: bool = True) -> None:
        self._responses = list(responses)
        self.supports_tools = supports_tools
        self.calls: list[dict] = []
        self.config: dict = {}
        self._active_tools: list = []

    # 方法作用：模拟 Runnable 配置包装，返回自身以保留调用记录
    # Args: config - 运行配置
    # Returns: 包装后的模型对象
    def with_config(self, config):
        self.config = {**self.config, **dict(config or {})}
        return self

    # 方法作用：模拟工具绑定，返回带工具列表的包装对象
    # Args: tools - 待绑定工具
    # Returns: 带工具的模型包装
    def bind_tools(self, tools):
        if not self.supports_tools:
            raise AttributeError("bind_tools 不受支持")
        return _BoundModel(self, tools)

    # 方法作用：模拟模型非流式调用并记录消息与配置
    # Args: messages - 消息序列；config - 运行配置
    # Returns: 脚本中的下一条响应
    async def ainvoke(self, messages, config=None):
        self.calls.append({
            "messages": list(messages), "config": config, "tools": list(self._active_tools),
        })
        return self.next_response()

    # 方法作用：模拟模型流式调用并逐块产出脚本分片
    # Args: messages - 消息序列；config - 运行配置
    # Returns: 分片异步生成器
    async def astream(self, messages, config=None):
        self.calls.append({
            "messages": list(messages), "config": config, "tools": list(self._active_tools),
        })
        for chunk in self.next_stream():
            yield chunk

    # 方法作用：弹出下一个已排队的响应对象。
    # Args: 无。
    # Returns: 脚本中的 AIMessage。
    def next_response(self) -> AIMessage:
        response = self._responses.pop(0)
        return response() if callable(response) else response

    # 方法作用：弹出下一个已排队的流式分片序列。
    # Args: 无。
    # Returns: AIMessageChunk 列表。
    def next_stream(self) -> list[AIMessageChunk]:
        response = self._responses.pop(0)
        return response() if callable(response) else response


# 方法作用：构造已注册 Prompt 的统一调用快照。
# Args: model - 测试模型；messages - 初始消息。
# Returns: PreparedInvocation。
def _prepared(model, messages: list | None = None) -> PreparedInvocation:
    return PreparedInvocation(
        prompt_id="sql.generate",
        prompt_version="1.0.0",
        task="generate_sql",
        model=model,
        messages=list(messages or [SystemMessage(content="系统提示"), HumanMessage(content="订单状态枚举")]),
        config={
            "metadata": {"prompt_id": "sql.generate", "request_id": "req-1"},
            "tags": ["prompt:sql.generate"],
        },
    )


# 方法作用：绑定一次执行用的知识循环配置，并让检索走内存假数据。
# Args: monkeypatch - pytest 补丁工具；results - 检索返回的证据；
#       search - 可选的外部检索 Mock，传入时不再新建；overrides - 配置覆盖项。
# Returns: 上下文管理器。
def _bind_loop(monkeypatch, results: list | None = None, search: AsyncMock | None = None, **overrides):
    import src.llm.knowledge_loop as loop_module
    import src.tools.knowledge_search as tool_module

    monkeypatch.setattr(
        tool_module,
        "search_knowledge",
        search if search is not None else AsyncMock(
            return_value=list(results if results is not None else [_evidence()])
        ),
        raising=False,
    )
    config = loop_module.KnowledgeLoopConfig(
        identity=_identity(),
        store_provider=AsyncMock(return_value=SimpleNamespace()),
        **overrides,
    )
    return loop_module.use_knowledge_loop(config)


class TestKnowledgeLoopInvoke:
    """覆盖功能 27.1.3 的非流式循环边界。"""

    # 方法作用：模型不请求工具时只有一次调用，且不触碰向量库。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_no_tool_call_keeps_single_model_call_and_zero_retrieval(self, monkeypatch) -> None:
        """模型不请求工具时只有一次调用，且不触碰向量库。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module
        import src.tools.knowledge_search as tool_module

        search = AsyncMock(return_value=[_evidence()])
        monkeypatch.setattr(tool_module, "search_knowledge", search, raising=False)
        model = _FakeToolModel([AIMessage(content="SELECT 1")])
        prepared = _prepared(model)

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(prepared)

        # Assert
        assert response.content == "SELECT 1"
        assert len(model.calls) == 1
        assert model.calls[0]["config"]["metadata"]["prompt_id"] == "sql.generate"
        search.assert_not_awaited()

    # 方法作用：工具回合必须写入匹配 tool_call_id 的 ToolMessage，再继续生成最终答案。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_tool_round_closes_ids_and_returns_final_answer(self, monkeypatch) -> None:
        """工具回合必须写入匹配 tool_call_id 的 ToolMessage，再继续生成最终答案。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": "search_knowledge",
                "args": {"query": "orders.status 枚举"},
                "id": "call-1",
            }]),
            AIMessage(content="SELECT status FROM orders"),
        ])
        prepared = _prepared(model)

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(prepared)

        # Assert
        assert response.content == "SELECT status FROM orders"
        assert len(model.calls) == 2
        second_round = model.calls[1]["messages"]
        tool_messages = [item for item in second_round if getattr(item, "type", "") == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call-1"
        assert "ok" in str(tool_messages[0].content)
        ai_messages = [item for item in second_round if getattr(item, "type", "") == "ai"]
        assert ai_messages[-1].tool_calls[0]["id"] == "call-1"

    # 方法作用：未知工具与非法参数都必须产生匹配 ID 的 ToolMessage，且不执行未知工具。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_unknown_and_invalid_tool_calls_still_close_ids(self, monkeypatch) -> None:
        """未知工具与非法参数都必须产生匹配 ID 的 ToolMessage，且不执行未知工具。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[
                {"name": "not_a_tool", "args": {}, "id": "call-unknown"},
                {"name": "search_knowledge", "args": {"query": "   "}, "id": "call-blank"},
                {"name": "search_knowledge", "args": {"query": "GMV", "top_k": 99}, "id": "call-range"},
                {"name": "search_knowledge", "args": {"query": "GMV", "tenant_id": 1}, "id": "call-extra"},
            ]),
            AIMessage(content="无法检索"),
        ])
        prepared = _prepared(model)

        # Act
        with _bind_loop(monkeypatch):
            await loop_module.invoke_with_knowledge(prepared)

        # Assert
        tool_messages = {
            item.tool_call_id: str(item.content)
            for item in model.calls[1]["messages"]
            if getattr(item, "type", "") == "tool"
        }
        assert set(tool_messages) == {"call-unknown", "call-blank", "call-range", "call-extra"}
        for payload in tool_messages.values():
            assert "invalid_arguments" in payload

    # 方法作用：最多 3 个工具回合，之后强制一次禁用工具的最终回答。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_tool_rounds_are_bounded_and_final_answer_disables_tools(self, monkeypatch) -> None:
        """最多 3 个工具回合，之后强制一次禁用工具的最终回答。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # 方法作用：覆盖 tool_round 的测试场景
        # Args: 无
        # Returns: 见方法内返回
        def tool_round():
            return AIMessage(content="", tool_calls=[{
                "name": "search_knowledge",
                "args": {"query": "GMV"},
                "id": "call-x",
            }])

        model = _FakeToolModel([
            tool_round, tool_round, tool_round, AIMessage(content="最终回答"),
        ])
        prepared = _prepared(model)

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(prepared)

        # Assert
        assert response.content == "最终回答"
        with_tools = [item for item in model.calls if item["tools"]]
        without_tools = [item for item in model.calls if not item["tools"]]
        assert len(with_tools) == 3
        assert len(without_tools) == 1

    # 方法作用：同一执行内重复查询命中缓存，不重复访问向量库。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_duplicate_query_is_served_from_cache(self, monkeypatch) -> None:
        """同一执行内重复查询命中缓存，不重复访问向量库。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module
        import src.tools.knowledge_search as tool_module

        search = AsyncMock(return_value=[_evidence()])
        monkeypatch.setattr(tool_module, "search_knowledge", search, raising=False)

        # 方法作用：覆盖 tool_round 的测试场景
        # Args: call_id - 用例参数
        # Returns: 见方法内返回
        def tool_round(call_id: str):
            return AIMessage(content="", tool_calls=[{
                "name": "search_knowledge",
                "args": {"query": "GMV 口径"},
                "id": call_id,
            }])

        model = _FakeToolModel([
            tool_round("call-1"),
            tool_round("call-2"),
            AIMessage(content="完成"),
        ])

        # Act
        with _bind_loop(monkeypatch, search=search):
            await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        assert search.await_count == 1

    # 方法作用：模型不支持 bind_tools 时回退为原有一次调用，且不冒充检索失败。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_bind_tools_fallback_when_unsupported(self, monkeypatch) -> None:
        """模型不支持 bind_tools 时回退为原有一次调用，且不冒充检索失败。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        model = _FakeToolModel([AIMessage(content="直接回答")], supports_tools=False)

        # Act
        with _bind_loop(monkeypatch):
            response = await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        assert response.content == "直接回答"
        assert len(model.calls) == 1
        assert model.calls[0]["tools"] == []

    # 方法作用：with_config 包装后 bind_tools 仍可用，且追踪配置不丢。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_bind_tools_preserves_tracking_config(self, monkeypatch) -> None:
        """with_config 包装后 bind_tools 仍可用，且追踪配置不丢。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        class TrackedModel:
            """模拟 RunnableBinding：暴露 config，但 bind_tools 返回的新包装不带配置。"""

            # 方法作用：初始化测试替身并记录调用容器
            # Args: 无
            # Returns: 无返回值
            def __init__(self, inner, config):
                self.bound = inner
                self.config = dict(config)

            # 方法作用：把未知属性转发给被包装模型，模拟 RunnableBinding 行为
            # Args: name - 属性名
            # Returns: 被包装模型的同名属性
            def __getattr__(self, name):
                return getattr(self.bound, name)

            # 方法作用：模拟 Runnable 配置包装，返回自身以保留调用记录
            # Args: config - 运行配置
            # Returns: 包装后的模型对象
            def with_config(self, config):
                return TrackedModel(self.bound, {**self.config, **dict(config or {})})

        inner = _FakeToolModel([AIMessage(content="SELECT 1")])
        initial = _prepared(inner)
        tracked = TrackedModel(inner, initial.config)

        # Act
        with _bind_loop(monkeypatch):
            await loop_module.invoke_with_knowledge(_prepared(tracked, initial.messages))

        # Assert
        recorded = inner.calls[0]
        assert recorded["config"]["metadata"]["prompt_id"] == "sql.generate"
        assert recorded["config"]["tags"] == ["prompt:sql.generate"]
        assert recorded["tools"]

    # 方法作用：取消必须向上传播，不能被通用异常处理吞掉。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_cancelled_error_propagates(self, monkeypatch) -> None:
        """取消必须向上传播，不能被通用异常处理吞掉。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        class CancellingModel(_FakeToolModel):
            # 方法作用：模拟模型非流式调用并记录消息与配置
            # Args: messages - 消息序列；config - 运行配置
            # Returns: 脚本中的下一条响应
            async def ainvoke(self, messages, config=None):
                raise asyncio.CancelledError()

        model = CancellingModel([])

        # Act + Assert
        with _bind_loop(monkeypatch):
            try:
                await loop_module.invoke_with_knowledge(_prepared(model))
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("CancelledError 必须向上传播")

    # 方法作用：消息预算不足时裁减工具证据正文，保持 tool_call_id 闭合。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_message_budget_trims_tool_evidence(self, monkeypatch) -> None:
        """消息预算不足时裁减工具证据正文，保持 tool_call_id 闭合。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        bulky = [_evidence(source_id=f"doc-{index}", content="口径" * 500) for index in range(6)]
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": "search_knowledge", "args": {"query": "GMV", "top_k": 6}, "id": "call-1",
            }]),
            AIMessage(content="完成"),
        ])

        # 预算必须容纳真实 schema 才可能挂上工具，这里保留恰好够 schema 加基础消息的空间。
        from src.tools.knowledge_search import build_knowledge_tools

        schema_chars = loop_module._tool_schema_chars(build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        ))

        # 预算刚好装得下 schema、一条结果预留与协议开销，但装不下 6 条 bulky 证据。
        tight_budget = (
            schema_chars
            + loop_module.TOOL_RESULT_RESERVE_CHARS
            + loop_module.TOOL_PROTOCOL_RESERVE_CHARS
            + 100
        )

        # Act
        with _bind_loop(monkeypatch, bulky, message_budget_chars=tight_budget):
            await loop_module.invoke_with_knowledge(_prepared(model))

        # Assert
        tool_messages = [
            item for item in model.calls[1]["messages"] if getattr(item, "type", "") == "tool"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call-1"
        assert len(str(tool_messages[0].content)) < 6000


class TestKnowledgeLoopStream:
    """覆盖功能 27.1.3/27.1.5 的流式协议边界。"""

    # 方法作用：中间工具回合的内容必须可被消费方丢弃，并带 SSE 过滤标记。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_marks_intermediate_round_and_resets(self, monkeypatch) -> None:
        """中间工具回合的内容必须可被消费方丢弃，并带 SSE 过滤标记。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module
        import src.tools.knowledge_search as tool_module

        model = _FakeToolModel([
            lambda: _tool_round_chunks("search_knowledge", "call-1", ['{"query":', '"GMV"}']),
            lambda: [AIMessageChunk(content="SELECT "), AIMessageChunk(content="1")],
        ])

        # Act
        yielded = []
        with _bind_loop(monkeypatch):
            async for chunk in loop_module.stream_with_knowledge(_prepared(model)):
                yielded.append(chunk)

        # Assert
        resets = [
            index for index, item in enumerate(yielded)
            if isinstance(item, loop_module.KnowledgeRoundReset)
        ]
        assert len(resets) == 1
        after = "".join(
            str(getattr(item, "content", "")) for item in yielded[resets[0] + 1:]
        )
        assert after == "SELECT 1"
        # 挂了工具的回合一律带 SSE 过滤标记，中间说明不会作为最终正文出现。
        assert loop_module.is_tool_round_tags(model.calls[0]["config"].get("tags"))
        assert loop_module.is_tool_round_tags(model.calls[1]["config"].get("tags"))
        # 工具参数分片必须完整合并后才执行，产生闭合的 ToolMessage。
        tool_messages = [
            item for item in model.calls[1]["messages"] if getattr(item, "type", "") == "tool"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call-1"
        assert tool_messages[0].name == "search_knowledge"
        assert '"status": "ok"' in str(tool_messages[0].content)
        # 分片合并后的 query 必须完整下推到检索层，而不是半截参数。
        search_call = tool_module.search_knowledge.await_args
        assert search_call.args[1] == "GMV"

    # 方法作用：工具回合耗尽后必须落到一次禁用工具的最终回答。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_forced_final_round_disables_tools(self, monkeypatch) -> None:
        """工具回合耗尽后必须落到一次禁用工具的最终回答。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # 方法作用：覆盖 chunks 的测试场景
        # Args: 无
        # Returns: 见方法内返回
        def chunks():
            return _tool_round_chunks("search_knowledge", "call-1", ['{"query":"GMV"}'])

        model = _FakeToolModel([
            chunks, chunks, chunks,
            lambda: [AIMessageChunk(content="最终回答")],
        ])

        # Act
        yielded = []
        with _bind_loop(monkeypatch):
            async for chunk in loop_module.stream_with_knowledge(_prepared(model)):
                yielded.append(chunk)

        # Assert
        assert model.calls[-1]["tools"] == []
        assert not loop_module.is_tool_round_tags(model.calls[-1]["config"].get("tags"))
        tail = yielded[yielded.index(yielded[-1])]
        assert getattr(tail, "content", "") == "最终回答"

    # 方法作用：无工具请求时按原样输出分片，不产生重置标记。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_without_tool_call_has_no_reset(self, monkeypatch) -> None:
        """无工具请求时按原样输出分片，不产生重置标记。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        model = _FakeToolModel([
            lambda: [AIMessageChunk(content="SELECT "), AIMessageChunk(content="1")],
        ])

        # Act
        yielded = []
        with _bind_loop(monkeypatch):
            async for chunk in loop_module.stream_with_knowledge(_prepared(model)):
                yielded.append(chunk)

        # Assert
        assert not any(isinstance(item, loop_module.KnowledgeRoundReset) for item in yielded)
        assert "".join(str(item.content) for item in yielded) == "SELECT 1"
        assert len(model.calls) == 1

    # 方法作用：流式回合的取消同样必须传播。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_stream_cancelled_error_propagates(self, monkeypatch) -> None:
        """流式回合的取消同样必须传播。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        class CancellingModel(_FakeToolModel):
            # 方法作用：模拟模型流式调用并逐块产出脚本分片
            # Args: messages - 消息序列；config - 运行配置
            # Returns: 分片异步生成器
            async def astream(self, messages, config=None):
                raise asyncio.CancelledError()
                yield AIMessageChunk(content="")  # pragma: no cover

        model = CancellingModel([])

        # Act + Assert
        with _bind_loop(monkeypatch):
            try:
                async for _ in loop_module.stream_with_knowledge(_prepared(model)):
                    pass
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("CancelledError 必须向上传播")


class TestKnowledgeLoopConfig:
    """覆盖功能 27.1.3/27.1.7 的服务端配置与身份绑定。"""

    # 方法作用：默认预算为 3 回合、6 次检索、单次 3 秒、累计 8 秒。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_config_defaults_are_bounded(self) -> None:
        """默认预算为 3 回合、6 次检索、单次 3 秒、累计 8 秒。"""
        # Arrange
        from src.llm.knowledge_loop import KnowledgeLoopConfig

        # Act
        config = KnowledgeLoopConfig()

        # Assert
        assert config.max_rounds == 3
        assert config.max_retrievals == 6
        assert config.per_retrieval_seconds == 3.0
        assert config.max_total_retrieval_seconds == 8.0

    # 方法作用：显式禁用时不构建任何检索工具。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_disabled_config_yields_no_tools(self) -> None:
        """显式禁用时不构建任何检索工具。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # Act
        with loop_module.use_knowledge_loop(loop_module.KnowledgeLoopConfig(enabled=False)):
            tools = loop_module.build_knowledge_tools_for_call(_prepared(_FakeToolModel([])))

        # Assert
        assert tools == []

    # 方法作用：后台绑定可信身份后必须能恢复原身份，避免串租户。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_background_identity_binding_is_reset(self) -> None:
        """后台绑定可信身份后必须能恢复原身份，避免串租户。"""
        # Arrange
        import src.llm.knowledge_loop as loop_module

        # Act
        with loop_module.use_trusted_identity(_identity(user_id=7), datasource="orders"):
            inside = loop_module.get_knowledge_loop_config()
            assert inside.datasource == "orders"
            assert inside.identity is not None and inside.identity.user_id == 7

        # Assert
        assert loop_module.get_knowledge_loop_config().datasource == ""
