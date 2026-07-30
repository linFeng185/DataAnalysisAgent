"""聊天 API 的租户 LLM 选择与请求级上下文测试。"""

from __future__ import annotations

import json
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

if TYPE_CHECKING:
    from src.llm.tenant_config import TenantLLMSelection


# 方法作用：构造不含真实凭证调用的固定租户 LLM 选择。
# Args: 无。
# Returns: tenant_id=7 的测试选择。
def _selection() -> TenantLLMSelection:
    from src.llm.tenant_config import TenantLLMSelection

    return TenantLLMSelection(
        tenant_id=7,
        connection_id=11,
        connection_name="primary",
        provider_code="openai",
        protocol="openai_compatible",
        model_catalog_id=21,
        model_id="gpt-4o",
        base_url="https://llm.example/v1",
        api_key="test-key",
    )


class TestChatLLMResolution:
    """覆盖功能 10.1.11 的默认选择、显式选择和失败关闭。"""

    # 方法作用：验证多租户未配置默认连接时拒绝对话。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_multi_tenant_missing_default_fails_closed(self, monkeypatch) -> None:
        """多租户模式不得回退平台 Settings 中的 API Key。"""
        # Arrange
        import src.api.auth as auth
        import src.app_context as app_context
        import src.llm.tenant_config as tenant_config
        from src.api.schemas import ChatRequest

        chat_routes = import_module("src.api.routes.chat")

        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(
            app_context,
            "get_tenant_policy",
            lambda: SimpleNamespace(multi_tenant=True),
        )
        monkeypatch.setattr(
            tenant_config,
            "resolve_tenant_llm_selection",
            AsyncMock(side_effect=LookupError("missing")),
        )

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await chat_routes._resolve_chat_llm_selection(ChatRequest(query="q"))
        assert caught.value.status_code == 409

    # 方法作用：验证单租户未配置租户连接时保留 Settings 兼容路径。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_single_tenant_missing_default_uses_settings_fallback(self, monkeypatch) -> None:
        """只有单租户且未显式选择时允许返回 None 走旧配置。"""
        # Arrange
        import src.api.auth as auth
        import src.app_context as app_context
        import src.llm.tenant_config as tenant_config
        from src.api.schemas import ChatRequest

        chat_routes = import_module("src.api.routes.chat")

        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 1)
        monkeypatch.setattr(
            app_context,
            "get_tenant_policy",
            lambda: SimpleNamespace(multi_tenant=False),
        )
        monkeypatch.setattr(
            tenant_config,
            "resolve_tenant_llm_selection",
            AsyncMock(side_effect=LookupError("missing")),
        )

        # Act
        result = await chat_routes._resolve_chat_llm_selection(ChatRequest(query="q"))

        # Assert
        assert result is None


class TestChatLLMContext:
    """覆盖功能 10.1.12 的非流式和 SSE ContextVar 生命周期。"""

    # 方法作用：验证非流式工作流执行期间可读取已授权租户选择。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_non_stream_workflow_receives_request_selection(self, monkeypatch) -> None:
        """`ainvoke` 内所有节点应统一使用当前对话连接。"""
        # Arrange
        import src.api.auth as auth
        import src.api.background_tasks as background_tasks
        import src.api.routes as routes_package
        import src.memory.session_store as session_store
        from src.api.schemas import ChatRequest
        from src.llm.tenant_config import get_current_tenant_llm_selection

        chat_routes = import_module("src.api.routes.chat")

        selection = _selection()
        observed = []

        class Workflow:
            """记录工作流执行时的 LLM 请求上下文。"""

            # 方法作用：模拟非流式 LangGraph 并读取 ContextVar。
            # Args: self - 工作流替身；state - 工作流状态；config - LangGraph 配置。
            # Returns: 最小成功响应。
            async def ainvoke(self, state, config):
                del self, state, config
                observed.append(get_current_tenant_llm_selection())
                return {"final_response": {"success": True}}

        # 方法作用：关闭测试生成的后台协程，避免访问真实 SessionStore。
        # Args: coroutine - 会话元数据协程；args/kwargs - 后台任务元数据。
        # Returns: None。
        def discard_background_task(coroutine, *args, **kwargs) -> None:
            del args, kwargs
            coroutine.close()

        monkeypatch.setattr(chat_routes, "_enforce_chat_request_quota", lambda req: None)
        monkeypatch.setattr(chat_routes, "_validated_skill_ids", lambda req: [])
        monkeypatch.setattr(
            chat_routes,
            "_resolve_chat_llm_selection",
            AsyncMock(return_value=selection),
        )
        monkeypatch.setattr(
            routes_package,
            "_resolve_chat_access",
            AsyncMock(return_value={"demo": {"allowed_columns": [], "row_filter_sql": ""}}),
        )
        monkeypatch.setattr(routes_package, "_app", lambda: Workflow())
        monkeypatch.setattr(background_tasks, "create_background_task", discard_background_task)
        monkeypatch.setattr(
            session_store,
            "get_session_store",
            lambda: SimpleNamespace(touch=AsyncMock()),
        )
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 71)
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")
        monkeypatch.setattr(auth, "scope_thread_id", lambda value: value)

        # Act
        await chat_routes.chat(ChatRequest(
            query="q",
            datasource="demo",
            session_id="session-1",
            llm_connection_id=11,
            model_id="gpt-4o",
        ))

        # Assert
        assert observed == [selection]
        assert get_current_tenant_llm_selection() is None

    # 方法作用：验证 SSE 的整个 astream_events 生命周期绑定租户选择。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_stream_workflow_receives_request_selection(self, monkeypatch) -> None:
        """流式生成器暂停和恢复时不得丢失或泄漏其他租户的选择。"""
        # Arrange
        import src.api.background_tasks as background_tasks
        import src.graph.workflow as workflow
        from src.api.streaming import stream_analysis
        from src.llm.tenant_config import get_current_tenant_llm_selection

        selection = _selection()
        observed = []

        class StreamingWorkflow:
            """记录流式工作流开始和恢复时的选择。"""

            # 方法作用：模拟两次异步事件边界并读取租户 LLM ContextVar。
            # Args: self - 工作流替身；args/kwargs - 工作流输入与配置。
            # Returns: 两个链事件的异步生成器。
            async def astream_events(self, *args, **kwargs):
                del self, args, kwargs
                observed.append(get_current_tenant_llm_selection())
                yield {"event": "on_chain_start", "name": "prepare_turn"}
                observed.append(get_current_tenant_llm_selection())
                yield {"event": "on_chain_end", "name": "prepare_turn", "data": {"output": {}}}

        # 方法作用：关闭测试生成的后台协程，避免访问真实 SessionStore。
        # Args: coroutine - 会话元数据协程；args/kwargs - 后台任务元数据。
        # Returns: None。
        def discard_background_task(coroutine, *args, **kwargs) -> None:
            del args, kwargs
            coroutine.close()

        monkeypatch.setattr(workflow, "app", StreamingWorkflow())
        monkeypatch.setattr(background_tasks, "create_background_task", discard_background_task)

        # Act
        events = [
            json.loads(chunk.removeprefix("data: ").strip())
            async for chunk in stream_analysis(
                "q",
                "demo",
                session_id="session-1",
                tenant_id=7,
                user_id=71,
                user_role="analyst",
                datasource_access={"demo": {"allowed_columns": [], "row_filter_sql": ""}},
                llm_selection=selection,
            )
        ]

        # Assert
        assert observed == [selection, selection]
        assert events[-1] == {"type": "done", "status": "complete"}
        assert get_current_tenant_llm_selection() is None


class TestTenantModelsRoute:
    """覆盖 `/models` 的单/多租户回退边界。"""

    # 方法作用：验证多租户无可用连接时返回空列表而非平台 Settings 模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_multi_tenant_models_do_not_fallback_to_settings(self, monkeypatch) -> None:
        """模型可见性必须直接由 multi_tenant 策略控制。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.management as management
        import src.app_context as app_context
        import src.llm.tenant_config as tenant_config

        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(
            app_context,
            "get_tenant_policy",
            lambda: SimpleNamespace(multi_tenant=True),
        )
        monkeypatch.setattr(
            tenant_config,
            "list_tenant_model_options",
            AsyncMock(return_value={"models": [], "default": {}}),
        )

        # Act
        result = await management.list_models()

        # Assert
        assert result == {"models": [], "default": {}}
