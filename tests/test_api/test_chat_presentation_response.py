"""覆盖 28.2/28.4：非流式接口不得丢弃节点已生成的公开展示元数据。"""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class TestChatPresentationResponse:
    """覆盖跨源字段标签从最终节点经过响应模型写回客户端的边界。"""

    # 复现真实非流式请求中的顶层元数据在响应构造与 Schema 序列化时丢失。
    # Args: self - 实例；monkeypatch - 后台与模型边界替身；language - 已解析语言；enabled - 管理员摘要快照。
    # Returns: 无返回值。
    @pytest.mark.parametrize("language,enabled", [("zh-CN", False), ("en-US", True)])
    async def test_nonstream_route_preserves_public_presentation(self, monkeypatch: pytest.MonkeyPatch, language: str, enabled: bool) -> None:
        """路由必须使用节点的语言与标签，而非界面默认值，且不能暴露原始推理。"""
        import src.api.background_tasks as tasks
        import src.api.routes as routes
        import src.memory.session_store as sessions
        from src.api.schemas import ChatRequest

        # Arrange
        chat = import_module("src.api.routes.chat")
        final = {"success": True, "display_language": language, "column_labels": {"sales_amount": "销售金额" if language == "zh-CN" else "Revenue"},
                 "process_summary_enabled": enabled, "data": [{"sales_amount": 10}], "reasoning_content": "内部推理不得出现在响应"}
        graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"final_response": final}))

        # 关闭测试生成的后台协程，不能访问真实历史数据库。
        # Args: coroutine - 元数据任务；args/kwargs - 调度参数。
        # Returns: 无返回值。
        def discard(coroutine, *args, **kwargs) -> None:
            coroutine.close()

        monkeypatch.setattr(chat, "_enforce_chat_request_quota", AsyncMock())
        monkeypatch.setattr(chat, "_resolve_chat_access_for_request", AsyncMock(return_value={}))
        monkeypatch.setattr(chat, "_resolve_chat_llm_selection", AsyncMock(return_value=None))
        monkeypatch.setattr(chat, "_validated_skill_ids", lambda request: [])
        monkeypatch.setattr(routes, "_app", lambda: graph)
        monkeypatch.setattr(tasks, "create_background_task", discard)
        monkeypatch.setattr(sessions, "get_session_store", lambda: SimpleNamespace(touch=AsyncMock()))
        # Act
        response = await chat.chat(ChatRequest(query="请用英文分析" if language == "en-US" else "跨源销售", session_id="test-response", display_language="zh-CN"))
        payload = response.model_dump()
        # Assert
        assert payload["display_language"] == language
        assert payload["column_labels"] == final["column_labels"]
        assert payload["process_summary_enabled"] is enabled
        assert payload["data"] == final["data"]
        assert "reasoning_content" not in payload

    # 验证旧调用方没有元数据时仍有安全默认，标签不共享可变对象。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_response_metadata_defaults_are_safe_and_independent(self) -> None:
        """旧结果仍兼容，缺少摘要开关时不能擅自显示，非法标签值应被拒绝。"""
        from pydantic import ValidationError

        from src.api.schemas import ChatResponse

        # Arrange / Act
        first = ChatResponse(success=True)
        second = ChatResponse(success=True)
        # Assert
        assert first.display_language == "zh-CN"
        assert first.process_summary_enabled is False
        first.column_labels["sales"] = "销售额"
        assert second.column_labels == {}
        with pytest.raises(ValidationError):
            ChatResponse(success=True, column_labels={"sales": ["非法标签"]})
