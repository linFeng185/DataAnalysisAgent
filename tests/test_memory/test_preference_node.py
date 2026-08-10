"""自然语言偏好写入节点回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
class TestPreferenceNode:
    """覆盖用户显式要求记忆偏好时的私有长期记忆写入。"""

    # 方法作用：验证图表偏好被规范化并按当前身份写入长期记忆。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_save_chart_preference_uses_current_identity(self, monkeypatch) -> None:
        """“请记住我偏好折线图”必须真实持久化，不能只返回模型文本。"""
        # Arrange
        import src.memory.long_term_store as store_module
        from src.graph.nodes.save_preference import save_preference_node

        store = SimpleNamespace(save_preference=AsyncMock())
        monkeypatch.setattr(store_module, "get_long_term_memory_store", AsyncMock(return_value=store))
        state = {
            "user_query": "请记住我偏好折线图",
            "tenant_id": 3,
            "user_id": 7,
            "user_role": "analyst",
        }

        # Act
        result = await save_preference_node(state)

        # Assert
        assert result["final_response"]["success"] is True
        assert result["final_response"]["source"] == "long_term_memory"
        args = store.save_preference.await_args
        assert args.args[:3] == (7, "chart_type", "line")
        assert args.kwargs["identity"].tenant_id == 3
        assert args.kwargs["identity"].user_id == 7

    # 方法作用：验证空偏好指令失败关闭且不访问存储。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_empty_preference_command_is_rejected(self, monkeypatch) -> None:
        """只有“记住”而没有偏好内容时不得创建垃圾长期记忆。"""
        # Arrange
        import src.memory.long_term_store as store_module
        from src.graph.nodes.save_preference import save_preference_node

        get_store = AsyncMock()
        monkeypatch.setattr(store_module, "get_long_term_memory_store", get_store)

        # Act
        result = await save_preference_node({
            "user_query": "请记住",
            "tenant_id": 3,
            "user_id": 7,
            "user_role": "analyst",
        })

        # Assert
        assert result["final_response"]["success"] is False
        assert result["final_response"]["status"] == "needs_input"
        get_store.assert_not_awaited()
