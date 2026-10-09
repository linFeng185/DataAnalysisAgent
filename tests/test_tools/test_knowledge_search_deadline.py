"""检索期限验收：只读路径的有界返回与在途任务收尾。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.security.tenant_policy import RequestIdentity


# 方法作用：构造测试用的可信请求身份。
# Args: 无。
# Returns: RequestIdentity。
def _identity() -> RequestIdentity:
    return RequestIdentity(tenant_id=2, user_id=7, role="analyst")


class _AbandonableSlowStore:
    """复刻 Milvus 只读路径：取消时放弃在途线程而不等待。"""

    # 方法作用：初始化测试替身并记录调用容器
    # Args: 无
    # Returns: 无返回值
    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.completed = False

    # 方法作用：模拟可放弃的同步只读查询，取消后立即让出。
    # Args: query - 检索文本；top_k - 结果数；filters - 过滤条件。
    # Returns: 空结果列表。
    async def search(self, query: str, top_k: int = 5, filters=None):
        task = asyncio.create_task(asyncio.to_thread(self._blocking))
        task.add_done_callback(self._consume)
        return await asyncio.shield(task)

    # 方法作用：收束被放弃任务的结果，避免未取回异常。
    # Args: task - 已完成或已取消的任务。
    # Returns: 无返回值。
    def _consume(self, task) -> None:
        if not task.cancelled():
            task.exception()

    # 方法作用：同步阻塞调用，模拟真实 SDK 的读操作。
    # Args: 无。
    # Returns: 空结果列表。
    def _blocking(self):
        time.sleep(self.delay)
        self.completed = True
        return []


class TestKnowledgeSearchDeadline:
    """覆盖 27.1.3/A6：工具期限必须真正有界，并留下可收尾的在途任务。"""

    # 方法作用：只读适配器可放弃时，工具在期限附近返回 timeout，不等满 SDK 调用。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_read_path_deadline_is_bounded(self, monkeypatch) -> None:
        """只读适配器可放弃时，工具在期限附近返回 timeout，不等满 SDK 调用。"""
        # Arrange
        import src.tools.knowledge_search as module

        store = _AbandonableSlowStore(delay=0.3)

        # 方法作用：把真实检索转发到可放弃的内存 store，只替换存储边界。
        # Args: _store - 工具传入的向量库；query - 检索文本；kwargs - 其余检索参数。
        # Returns: 命中的证据列表。
        async def fake_search(_store, query, **kwargs):
            return await store.search(query)

        monkeypatch.setattr(module, "search_knowledge", fake_search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            per_retrieval_seconds=0.05,
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        started = time.monotonic()
        payload = await module.execute_knowledge_search(
            tools[0], {"query": "status"}, identity=_identity(),
        )
        elapsed = time.monotonic() - started

        # Assert：状态明确为 timeout，且墙钟被期限约束而不是等满 300ms。
        assert payload["status"] == "timeout"
        assert payload["evidence"] == []
        assert elapsed < 0.2
        # 被放弃的在途线程最终自行结束，不留无限后台任务。
        await asyncio.sleep(0.35)
        assert store.completed is True

    # 方法作用：超时同样计入一次真实尝试，第二次请求必须被预算阻断。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_timeout_consumes_budget(self, monkeypatch) -> None:
        """超时同样计入一次真实尝试，第二次请求必须被预算阻断。"""
        # Arrange
        import src.tools.knowledge_search as module

        store = _AbandonableSlowStore(delay=0.3)

        # 方法作用：把真实检索转发到可放弃的内存 store，只替换存储边界。
        # Args: _store - 工具传入的向量库；query - 检索文本；kwargs - 其余检索参数。
        # Returns: 命中的证据列表。
        async def fake_search(_store, query, **kwargs):
            return await store.search(query)

        monkeypatch.setattr(module, "search_knowledge", fake_search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            per_retrieval_seconds=0.05,
            max_retrievals=1,
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        first = await module.execute_knowledge_search(
            tools[0], {"query": "status"}, identity=_identity(),
        )
        second = await module.execute_knowledge_search(
            tools[0], {"query": "status2"}, identity=_identity(),
        )

        # Assert
        assert first["status"] == "timeout"
        assert second["status"] == "budget_exhausted"
        await asyncio.sleep(0.35)

    # 方法作用：外层取消必须向上传播，而不是被转换成 timeout 状态。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_cancel_propagates_from_retrieval(self, monkeypatch) -> None:
        """外层取消必须向上传播，而不是被转换成 timeout 状态。"""
        # Arrange
        import src.tools.knowledge_search as module

        # 方法作用：模拟检索层传播取消，验证工具不会把它降级成状态码。
        # Args: _store - 工具传入的向量库；query - 检索文本；kwargs - 其余检索参数。
        # Returns: 不会返回，始终抛 CancelledError。
        async def cancelling_search(_store, query, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(module, "search_knowledge", cancelling_search, raising=False)
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act + Assert
        try:
            await module.execute_knowledge_search(
                tools[0], {"query": "status"}, identity=_identity(),
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("CancelledError 必须向上传播")
