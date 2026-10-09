"""跨方言知识→SQL 确定性链路：Fake 模型 + 真实工具与检索层，无远程服务。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.llm.invocation import PreparedInvocation
from src.memory.vector_store import VectorSearchResult
from src.security.tenant_policy import RequestIdentity

# 知识库中的权威枚举：模型必须用这份取值构造过滤条件，而不是猜测。
VALID_STATUSES = ("paid", "cancelled")


class _MemoryStore:
    """隔离内存向量库替身：只实现检索接口，不触达任何远程服务。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, results: list[VectorSearchResult]) -> None:
        self._results = list(results)
        self.search_calls = 0

    # 方法作用：返回预置命中，模拟真实向量召回。
    # Args: query - 检索文本；top_k - 返回条数；filters - metadata 过滤。
    # Returns: 预置的命中列表。
    async def search(self, query: str, top_k: int = 5, filters=None):
        self.search_calls += 1
        return list(self._results[:top_k])

    # 方法作用：关键词通道返回空，避免干扰确定性断言。
    # Args: filters - metadata 过滤；limit - 返回条数。
    # Returns: 空列表。
    async def get_by_filter(self, filters=None, limit: int = 100):
        return []


# 方法作用：构造包含 orders.status 枚举的隔离内存向量库。
# Args: 无。
# Returns: 内存 store 替身。
def _memory_store() -> _MemoryStore:
    return _MemoryStore([
        VectorSearchResult(
            id="doc-orders-status",
            content="orders.status 的合法取值为 paid 和 cancelled",
            metadata={
                "category": "enum_value",
                "source": "manual_doc",
                "column_name": "status",
                "table_name": "orders",
                "document_version": "v3",
                "locator_json": '{"page": 7}',
            },
            score=0.95,
        ),
    ])


class _BoundToolModel:
    """带工具的模型包装，转发到同一个脚本模型。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, inner: "_FakeToolModel", tools: list) -> None:
        self._inner = inner
        self.tools = list(tools)

    # 方法作用：模拟 Runnable 配置包装，返回自身以保留调用记录
    # Args: config - 运行配置
    # Returns: 包装后的模型对象
    def with_config(self, config):
        return self

    # 方法作用：记录带工具的模型调用并返回脚本响应。
    # Args: messages - 消息序列；config - 运行配置。
    # Returns: 脚本中的下一条响应。
    async def ainvoke(self, messages, config=None):
        self._inner.calls.append({
            "messages": list(messages), "config": config, "tools": self.tools,
        })
        return self._inner._responses.pop(0)


class _FakeToolModel:
    """两轮脚本模型：先请求知识工具，再用返回口径生成 SQL。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, sql: str) -> None:
        self._responses = [
            AIMessage(content="", tool_calls=[{
                "name": "search_knowledge",
                "args": {"query": "orders.status 合法值", "category": "enum_value"},
                "id": "call-status",
            }]),
            AIMessage(content=sql),
        ]
        self.calls: list[dict] = []
        self.config: dict = {}

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
        return _BoundToolModel(self, tools)

    # 方法作用：记录一次未绑工具的模型调用（最终回答路径）。
    # Args: messages - 消息序列；config - 运行配置。
    # Returns: 脚本中的下一条响应。
    async def ainvoke(self, messages, config=None):
        self.calls.append({"messages": list(messages), "config": config, "tools": []})
        return self._responses.pop(0)


# 方法作用：构造统一调用快照，消息结构与真实节点一致。
# Args: model - 测试模型。
# Returns: PreparedInvocation。
def _prepared(model) -> PreparedInvocation:
    return PreparedInvocation(
        prompt_id="sql.generate",
        prompt_version="1.0.0",
        task="generate_sql",
        model=model,
        messages=[SystemMessage(content="系统提示"), HumanMessage(content="统计各状态订单数")],
        config={"metadata": {"prompt_id": "sql.generate"}, "tags": ["prompt:sql.generate"]},
    )


class TestKnowledgeToSqlCrossDialect:
    """覆盖 27.1.1/27.1.3/27.1.8：知识口径必须真实进入 SQL 条件并通过方言校验。"""

    # 方法作用：Fake 模型请求枚举 → 真实工具与检索层返回证据 → 续轮 SQL 通过既有校验。
    # Args: monkeypatch - pytest 内置夹具；dialect - 用例参数；sql - 用例参数
    # Returns: 无返回值，断言失败由 pytest 报告
    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("clickhouse", "SELECT status, count() FROM orders "
                           "WHERE status IN ('paid','cancelled') GROUP BY status"),
            ("mysql", "SELECT status, COUNT(*) FROM orders "
                      "WHERE status IN ('paid','cancelled') GROUP BY status"),
            ("postgres", "SELECT status, COUNT(*) FROM orders "
                         "WHERE status IN ('paid','cancelled') GROUP BY status"),
        ],
    )
    async def test_knowledge_round_feeds_sql_and_passes_validation(
        self, monkeypatch, dialect: str, sql: str,
    ) -> None:
        """Fake 模型请求枚举 → 真实工具与检索层返回证据 → 续轮 SQL 通过既有校验。"""
        # Arrange
        import src.tools.knowledge_search as tool_module
        from src.api.auth import use_identity_context
        from src.llm.knowledge_loop import KnowledgeLoopConfig, invoke_with_knowledge
        from src.llm.knowledge_loop import use_knowledge_loop as bind_loop
        from src.security.sql_schema_validation import validate_sql_schema
        from src.tools.sqlglot_validator import validate_with_sqlglot

        store = _memory_store()
        identity = RequestIdentity(tenant_id=2, user_id=7, role="analyst")
        # 只替换服务端数据源授权边界；工具、检索、重排与循环都走真实实现。
        monkeypatch.setattr(
            tool_module, "resolve_datasource_authorization",
            AsyncMock(return_value=True), raising=False,
        )
        model = _FakeToolModel(sql)
        loop_config = KnowledgeLoopConfig(
            identity=identity,
            datasource="orders",
            allowed_datasources=frozenset({"orders"}),
            store_provider=AsyncMock(return_value=store),
        )

        # Act
        with bind_loop(loop_config), use_identity_context(identity):
            response = await invoke_with_knowledge(_prepared(model))

        # Assert：模型两次调用；工具消息真实闭合且带来源引用。
        assert len(model.calls) == 2
        tool_messages = [
            item for item in model.calls[1]["messages"] if getattr(item, "type", "") == "tool"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call-status"
        payload = json.loads(str(tool_messages[0].content))
        assert payload["status"] == "ok"
        assert payload["citations"][0]["source_id"] == "doc-orders-status"
        assert payload["citations"][0]["version"] == "v3"
        # 真实检索层按 system/tenant/private 三个隔离范围各召回一次。
        assert store.search_calls == 3
        assert all(status in payload["evidence"][0]["content"] for status in VALID_STATUSES)

        # Assert：生成的 SQL 使用知识库口径，并通过方言只读校验与列归属校验。
        assert response.content == sql
        validation = validate_with_sqlglot(sql, dialect)
        assert validation["valid"] is True, validation["errors"]
        issues = validate_sql_schema(
            sql,
            datasources=["orders"],
            dialect=dialect,
            tables=[{
                "name": "orders",
                "datasource": "orders",
                "columns": [{"name": "status"}, {"name": "amount"}],
            }],
        )
        assert [item for item in issues if item.confidence == "high"] == []
