"""SSE 工具回合隔离回归：中间正文不得作为最终答案增量下发。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, AIMessageChunk

from src.llm.knowledge_loop import TOOL_ROUND_TAG


class _FakeApp:
    """按脚本回放 LangGraph 事件序列，不启动真实工作流。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    # 方法作用：按顺序产出预设事件，模拟 astream_events v2 协议。
    # Args: input_state - 图输入；config - 运行配置；version - 事件协议版本。
    # Returns: 事件异步生成器。
    async def astream_events(self, input_state, config, version="v2"):
        for event in self._events:
            yield event


# 方法作用：构造一条携带知识工具标记的模型流式事件。
# Args: kind - 事件类型；run_id - 模型调用标识；node - 所属节点；extra - 事件负载字段。
# Returns: astream_events 形状的事件字典。
def _event(kind: str, run_id: str, node: str = "generate_sql", **extra) -> dict:
    event = {
        "event": kind,
        "run_id": run_id,
        "name": "ChatOpenAI",
        "tags": [TOOL_ROUND_TAG],
        "metadata": {"langgraph_node": node},
        "data": {},
    }
    event.update(extra)
    return event


# 方法作用：解析 SSE 原始报文为 (事件名, 负载) 列表。
# Args: chunks - stream_analysis 产出的字符串分片。
# Returns: 解析后的事件列表。
def _parse_sse(chunks: list[str]) -> list[tuple[str, dict]]:
    parsed: list[tuple[str, dict]] = []
    for chunk in chunks:
        for block in str(chunk).split("\n\n"):
            line = block.strip()
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:"):].strip())
            parsed.append((str(payload.get("type", "")), payload))
    return parsed


# 方法作用：把后台元数据任务替换成不触达数据库的空实现。
# Args: monkeypatch - pytest 补丁工具。
# Returns: 无返回值。
def _stub_background(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.background_tasks.create_background_task",
        lambda coro, **kwargs: coro.close(),
    )
    import src.api.streaming as streaming

    monkeypatch.setattr(streaming, "_save_session_meta", AsyncMock())
    monkeypatch.setattr(streaming, "_touch_session_meta", AsyncMock())


# 方法作用：消费一次 SSE 流并收集全部字符串分片。
# Args: **kwargs - stream_analysis 参数。
# Returns: SSE 分片列表。
async def _collect(**kwargs) -> list[str]:
    import src.api.streaming as streaming

    chunks: list[str] = []
    async for chunk in streaming.stream_analysis(**kwargs):
        chunks.append(chunk)
    return chunks


class TestStreamToolRoundIsolation:
    """覆盖 27.1.5：工具回合正文与最终答案在同一节点内必须分离。"""

    # 方法作用：带工具标记的回合先缓冲，确认无 tool_calls 后才释放为最终 token。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_intermediate_tokens_are_withheld_until_round_is_final(self, monkeypatch) -> None:
        """带工具标记的回合先缓冲，确认无 tool_calls 后才释放为最终 token。"""
        # Arrange
        import src.graph.workflow as workflow

        events = [
            {"event": "on_chain_start", "name": "generate_sql", "run_id": "n1",
             "tags": [], "metadata": {}, "data": {}},
            _event("on_chat_model_start", "r1"),
            _event("on_chat_model_stream", "r1",
                   data={"chunk": AIMessageChunk(content="中间说明不得下发")}),
            _event("on_chat_model_end", "r1", data={"output": AIMessage(
                content="中间说明不得下发",
                tool_calls=[{"name": "search_knowledge", "args": {"query": "status"}, "id": "c1"}],
            )}),
            {"event": "on_tool_start", "name": "search_knowledge", "run_id": "t1",
             "tags": [], "metadata": {"langgraph_node": "generate_sql"}, "data": {}},
            _event("on_chat_model_start", "r2"),
            _event("on_chat_model_stream", "r2",
                   data={"chunk": AIMessageChunk(content="SELECT 1")}),
            _event("on_chat_model_end", "r2", data={"output": AIMessage(content="SELECT 1")}),
            {"event": "on_chain_end", "name": "generate_sql", "run_id": "n1",
             "tags": [], "metadata": {}, "data": {"output": {"generated_sql": "SELECT 1"}}},
        ]
        monkeypatch.setattr(workflow, "app", _FakeApp(events))
        _stub_background(monkeypatch)

        # Act
        parsed = _parse_sse(await _collect(
            user_query="订单状态枚举", datasource="orders", session_id="sess-review",
            datasources=["orders"], tenant_id=2, user_id=7, user_role="analyst",
            datasource_access={"orders": {"name": "orders"}},
        ))

        # Assert：只有最终答案成为 token，llm_start/llm_end 与 done 仍然保留。
        tokens = [payload["content"] for name, payload in parsed if name == "token"]
        assert tokens == ["SELECT 1"]
        assert all("中间说明" not in token for token in tokens)
        names = [name for name, _ in parsed]
        assert names.count("llm_start") == 2
        assert names.count("llm_end") == 2
        assert names[-1] == "done"
        assert parsed[-1][1].get("status") == "complete"

    # 方法作用：无工具标记的回合按增量实时下发，只出现一次。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_final_round_without_tools_streams_once(self, monkeypatch) -> None:
        """无工具标记的回合按增量实时下发，只出现一次。"""
        # Arrange
        import src.graph.workflow as workflow

        events = [
            {"event": "on_chain_start", "name": "llm_direct_answer", "run_id": "n2",
             "tags": [], "metadata": {}, "data": {}},
            {"event": "on_chat_model_start", "run_id": "r3", "name": "ChatOpenAI",
             "tags": [], "metadata": {"langgraph_node": "llm_direct_answer"}, "data": {}},
            {"event": "on_chat_model_stream", "run_id": "r3", "name": "ChatOpenAI",
             "tags": [], "metadata": {"langgraph_node": "llm_direct_answer"},
             "data": {"chunk": AIMessageChunk(content="直接回答")}},
            {"event": "on_chat_model_end", "run_id": "r3", "name": "ChatOpenAI",
             "tags": [], "metadata": {"langgraph_node": "llm_direct_answer"},
             "data": {"output": AIMessage(content="直接回答")}},
            {"event": "on_chain_end", "name": "llm_direct_answer", "run_id": "n2",
             "tags": [], "metadata": {}, "data": {"output": {}}},
        ]
        monkeypatch.setattr(workflow, "app", _FakeApp(events))
        _stub_background(monkeypatch)

        # Act
        parsed = _parse_sse(await _collect(
            user_query="你好", datasource="", session_id="sess-review-2", datasources=[],
            tenant_id=2, user_id=7, user_role="analyst", datasource_access={},
        ))

        # Assert
        tokens = [payload["content"] for name, payload in parsed if name == "token"]
        assert tokens == ["直接回答"]

    # 方法作用：知识工具标记不能被父节点推断误认成节点名。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_tool_round_tag_is_not_treated_as_node_name(self) -> None:
        """知识工具标记不能被父节点推断误认成节点名。"""
        # Arrange
        from src.api.streaming import _find_parent_node

        # Act
        node = _find_parent_node({"tags": [TOOL_ROUND_TAG], "metadata": {}})

        # Assert
        assert node is None

    # 方法作用：请求级知识范围必须绑定并在流结束后复位，避免跨请求泄漏。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_knowledge_scope_is_reset_after_stream(self, monkeypatch) -> None:
        """请求级知识范围必须绑定并在流结束后复位，避免跨请求泄漏。"""
        # Arrange
        import src.graph.workflow as workflow
        from src.llm import knowledge_loop

        monkeypatch.setattr(workflow, "app", _FakeApp([
            {"event": "on_chain_start", "name": "generate_sql", "run_id": "n3",
             "tags": [], "metadata": {}, "data": {}},
        ]))
        _stub_background(monkeypatch)
        seen: dict[str, object] = {}

        # Act：在流消费过程中观察绑定值，流结束后观察是否复位。
        import src.api.streaming as streaming

        observed: list[str] = []
        async for _ in streaming.stream_analysis(
            user_query="订单", datasource="orders", session_id="sess-scope",
            datasources=["orders"], tenant_id=2, user_id=7, user_role="analyst",
            datasource_access={"orders": {"name": "orders"}},
        ):
            observed.append(knowledge_loop.get_knowledge_loop_config().datasource)
        seen["datasource"] = knowledge_loop.get_knowledge_loop_config().datasource

        # Assert：流执行期间绑定生效，流结束后必须复位。
        assert "orders" in observed
        assert seen["datasource"] == ""

    # 方法作用：绑定帮助函数必须返回可复位 Token，供生成器 finally 使用。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_scope_token_is_bound_before_iteration(self) -> None:
        """绑定帮助函数必须返回可复位 Token，供生成器 finally 使用。"""
        # Arrange
        from src.llm.knowledge_loop import (
            bind_request_knowledge_scope,
            get_knowledge_loop_config,
            reset_knowledge_loop,
        )

        # Act
        token = bind_request_knowledge_scope("orders", frozenset({"orders"}))

        # Assert
        config = get_knowledge_loop_config()
        assert config.datasource == "orders"
        assert config.identity is None
        reset_knowledge_loop(token)
        assert get_knowledge_loop_config().datasource == ""
