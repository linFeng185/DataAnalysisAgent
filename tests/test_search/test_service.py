"""受控联网搜索服务测试：门禁、工具选择、单次调用预算、超时取消、有界解析与日志脱敏。

覆盖功能 32.1、32.2（设计与边界见 spec/58-controlled-web-search.md）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

logger = logging.getLogger(__name__)

_SEARCH_TOOL_NAME = "public_search__web_search"
_QUERY = "联网搜索 受控联网测试主题"


# 方法作用：构造显式开启联网搜索的测试配置。
# Args: overrides - 需要覆盖的字段。
# Returns: 已完成校验的 Settings 实例。
def _make_settings(**overrides):
    from src.config import Settings

    base: dict = {
        "web_search_enabled": True,
        "web_search_mcp_tools": _SEARCH_TOOL_NAME,
        "web_search_timeout_seconds": 5.0,
        "web_search_max_results": 8,
        "web_search_max_response_chars": 20_000,
        "web_search_max_query_chars": 256,
    }
    base.update(overrides)
    return Settings(**base)


# 方法作用：构造带真实参数 Schema 的 LangChain 测试搜索工具。
# Args: name - 完整工具名；limit_field - 限额字段名（max_results/limit/空）；extra_fields - 额外字段定义；handler - 返回原始响应的异步处理函数。
# Returns: (StructuredTool, 调用记录列表) 二元组。
def _make_langchain_tool(
    name: str,
    *,
    limit_field: str = "max_results",
    extra_fields: dict | None = None,
    handler=None,
):
    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    calls: list[dict] = []

    # 方法作用：记录一次真实工具调用并返回测试响应。
    # Args: kwargs - 经 Schema 校验后的工具参数。
    # Returns: 原始响应文本或等价载荷。
    async def _call(**kwargs):
        calls.append(dict(kwargs))
        if handler is not None:
            return await handler(kwargs)
        return json.dumps({"results": []})

    fields: dict = {"query": (str, Field(...))}
    if limit_field:
        fields[limit_field] = (int | None, Field(None))
    if extra_fields:
        fields.update(extra_fields)
    schema = create_model("ToolArgs", **fields)
    tool = StructuredTool(
        name=name,
        description="测试搜索工具",
        coroutine=_call,
        args_schema=schema,
    )
    return tool, calls


# 方法作用：构造只暴露指定 Schema 的伪工具（用于 Schema 兼容性边界）。
# Args: name - 完整工具名；args_schema - 工具参数 Schema。
# Returns: SimpleNamespace 伪工具，受测代码只读取 name/args_schema/ainvoke。
def _make_stub_tool(name: str, args_schema) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        args_schema=args_schema,
        ainvoke=AsyncMock(return_value=json.dumps({"results": []})),
    )


# 方法作用：把固定文本包装为异步工具响应。
# Args: text - 原始响应文本。
# Returns: 协程结果文本。
async def _text(text: str) -> str:
    return text


# 方法作用：把字典包装为异步工具响应（等价字典载荷形态）。
# Args: payload - 响应字典。
# Returns: 原样字典。
async def _dict(payload: dict) -> dict:
    return payload


# 方法作用：为被测服务注入配置、身份与 MCP 管理器替身。
# Args: monkeypatch - pytest 补丁工具；settings - 测试配置；tools - 管理器返回的已加载工具；user_id/tenant_id/role - 模拟认证身份。
# Returns: (manager 替身, factory 替身) 二元组。
def _patch_service(
    monkeypatch,
    *,
    settings,
    tools=(),
    user_id: int = 7,
    tenant_id: int = 4,
    role: str = "analyst",
):
    import src.search.service as service_module

    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(service_module, "get_current_user_id", lambda: user_id)
    monkeypatch.setattr(service_module, "get_current_tenant_id", lambda: tenant_id)
    monkeypatch.setattr(service_module, "get_current_role", lambda: role)
    manager = SimpleNamespace(
        get_all_tools=MagicMock(return_value=list(tools)),
        ensure_scoped_servers=AsyncMock(),
    )
    factory = MagicMock(return_value=manager)
    monkeypatch.setattr(service_module, "get_mcp_client_manager", factory)
    return manager, factory


# 方法作用：断言日志替身的所有调用记录都不包含敏感片段。
# Args: fake_logger - MagicMock 日志替身；needles - 禁止出现的敏感片段。
# Returns: 无返回值，泄漏时由断言报告。
def _assert_logs_are_clean(fake_logger: MagicMock, *needles: str) -> None:
    for call in fake_logger.method_calls:
        rendered = str(call)
        for needle in needles:
            assert needle not in rendered, f"日志泄漏敏感片段: {needle}"


# 方法作用：构造成功搜索响应文本。
# Args: urls - 期望返回的来源 URL 列表。
# Returns: 合法 JSON 响应文本。
def _results_payload(urls: list[str]) -> str:
    return json.dumps({
        "results": [
            {"title": f"标题{index}", "url": url, "snippet": f"摘要{index}"}
            for index, url in enumerate(urls)
        ]
    })


class TestGateAndZeroInitialization:
    """覆盖功能 32.1：三态开关与零初始化边界。"""

    # 方法作用：验证默认关闭时直接返回 disabled 且不触碰任何工具或连接。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，触达连接层时由断言报告。
    async def test_disabled_default_never_touches_mcp(self, monkeypatch) -> None:
        """默认关闭必须零搜索、零新增模型调用、零连接初始化。"""
        logger.debug("test_disabled_default_never_touches_mcp 入口")
        try:
            # Arrange
            import src.search.service as service_module

            settings = _make_settings(web_search_enabled=False)
            manager, factory = _patch_service(monkeypatch, settings=settings)

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "disabled"
            factory.assert_not_called()
            manager.get_all_tools.assert_not_called()
            logger.info("test_disabled_default_never_touches_mcp 完成")
        except Exception as exc:
            logger.error("test_disabled_default_never_touches_mcp 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证开启但缺少明确意图时不读取工具也不建立连接。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，越界触达时由断言报告。
    async def test_enabled_without_explicit_intent_skips_tools(self, monkeypatch) -> None:
        """「市场研究」类问法不构成外送授权。"""
        logger.debug("test_enabled_without_explicit_intent_skips_tools 入口")
        try:
            # Arrange
            import src.search.service as service_module

            settings = _make_settings()
            manager, factory = _patch_service(monkeypatch, settings=settings)

            # Act
            response = await service_module.search_public_web("帮我做竞品研究")

            # Assert
            assert response.success is False
            assert response.error_code == "no_explicit_intent"
            factory.assert_not_called()
            manager.get_all_tools.assert_not_called()
            logger.info("test_enabled_without_explicit_intent_skips_tools 完成")
        except Exception as exc:
            logger.error("test_enabled_without_explicit_intent_skips_tools 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证空问法与超长问法都在长度门禁拒绝且不触碰工具层。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；query - 越界问法。
    # Returns: 无返回值，越界触达时由断言报告。
    @pytest.mark.parametrize(
        "query",
        [None, "", "   ", "联网搜索 " + ("长" * 300)],
    )
    async def test_invalid_query_rejected_before_tool_access(self, monkeypatch, query) -> None:
        """纯空白与超限问题拒绝处理，不截断用户意图。"""
        logger.debug("test_invalid_query_rejected_before_tool_access 入口")
        try:
            # Arrange
            import src.search.service as service_module

            settings = _make_settings()
            manager, factory = _patch_service(monkeypatch, settings=settings)

            # Act
            response = await service_module.search_public_web(query)

            # Assert
            assert response.success is False
            assert response.error_code in {"invalid_query", "no_explicit_intent"}
            factory.assert_not_called()
            manager.get_all_tools.assert_not_called()
            logger.info("test_invalid_query_rejected_before_tool_access 完成")
        except Exception as exc:
            logger.error("test_invalid_query_rejected_before_tool_access 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证匿名身份在工具枚举前被拒绝。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，匿名触达工具层时由断言报告。
    async def test_anonymous_identity_rejected(self, monkeypatch) -> None:
        """匿名请求（user_id<=0 或 role=anonymous）不得触发任何搜索。"""
        logger.debug("test_anonymous_identity_rejected 入口")
        try:
            # Arrange
            import src.search.service as service_module

            settings = _make_settings()
            _manager, factory = _patch_service(monkeypatch, settings=settings, user_id=0, role="anonymous")

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "unauthorized"
            factory.assert_not_called()
            logger.info("test_anonymous_identity_rejected 完成")
        except Exception as exc:
            logger.error("test_anonymous_identity_rejected 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证服务端角色为 anonymous 时即使 user_id 异常为正数也拒绝。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，角色门禁失效时由断言报告。
    async def test_anonymous_role_rejected_even_with_positive_user(self, monkeypatch) -> None:
        """角色与用户编号必须同时来自真实认证上下文。"""
        logger.debug("test_anonymous_role_rejected_even_with_positive_user 入口")
        try:
            # Arrange
            import src.search.service as service_module

            settings = _make_settings()
            _manager, factory = _patch_service(monkeypatch, settings=settings, user_id=9, role="anonymous")

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.error_code == "unauthorized"
            factory.assert_not_called()
            logger.info("test_anonymous_role_rejected_even_with_positive_user 完成")
        except Exception as exc:
            logger.error("test_anonymous_role_rejected_even_with_positive_user 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证空白名单返回 no_tool 且不为搜索请求建立连接。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，触达连接层时由断言报告。
    async def test_empty_whitelist_returns_no_tool_without_connection(self, monkeypatch) -> None:
        """白名单为空或全为通配符时不得全量建立 MCP 连接。"""
        logger.debug("test_empty_whitelist_returns_no_tool_without_connection 入口")
        try:
            # Arrange
            import src.search.service as service_module

            settings = _make_settings(web_search_mcp_tools=" , public_search__* , ")
            _manager, factory = _patch_service(monkeypatch, settings=settings)

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "no_tool"
            factory.assert_not_called()
            logger.info("test_empty_whitelist_returns_no_tool_without_connection 完成")
        except Exception as exc:
            logger.error("test_empty_whitelist_returns_no_tool_without_connection 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestToolSelection:
    """覆盖功能 32.2：精确白名单、参数 Schema 与调用参数。"""

    # 方法作用：验证兼容 Schema 工具收到原始 trim 问法与服务端限额，且只调用一次。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，参数不符时由断言报告。
    async def test_compatible_schema_receives_original_query_and_server_limit(self, monkeypatch) -> None:
        """模型不能改写出站请求：参数只能是原始问法和服务端限额。"""
        logger.debug("test_compatible_schema_receives_original_query_and_server_limit 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, calls = _make_langchain_tool(_SEARCH_TOOL_NAME)
            settings = _make_settings(web_search_max_results=6)
            manager, _ = _patch_service(monkeypatch, settings=settings, tools=[tool])

            # Act
            response = await service_module.search_public_web(f"  {_QUERY}  ")

            # Assert
            assert response.success is True
            assert calls == [{"query": _QUERY, "max_results": 6}]
            manager.ensure_scoped_servers.assert_not_awaited()
            logger.info("test_compatible_schema_receives_original_query_and_server_limit 完成")
        except Exception as exc:
            logger.error("test_compatible_schema_receives_original_query_and_server_limit 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证可选 limit:integer 变体同样被识别并使用 limit 字段名传参。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，参数名不符时由断言报告。
    async def test_limit_variant_schema_supported(self, monkeypatch) -> None:
        """既有工具可选 int|null 参数必须以原名接收服务端限额。"""
        logger.debug("test_limit_variant_schema_supported 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, calls = _make_langchain_tool(_SEARCH_TOOL_NAME, limit_field="limit")
            settings = _make_settings(web_search_max_results=3)
            _patch_service(monkeypatch, settings=settings, tools=[tool])

            # Act
            await service_module.search_public_web(_QUERY)

            # Assert
            assert calls == [{"query": _QUERY, "limit": 3}]
            logger.info("test_limit_variant_schema_supported 完成")
        except Exception as exc:
            logger.error("test_limit_variant_schema_supported 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证无限制字段的纯 query Schema 是合法形态。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，拒绝合法 Schema 时由断言报告。
    async def test_query_only_schema_supported(self, monkeypatch) -> None:
        """只含必需 query:string 是最小兼容 Schema。"""
        logger.debug("test_query_only_schema_supported 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, calls = _make_langchain_tool(_SEARCH_TOOL_NAME, limit_field="")
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            assert calls == [{"query": _QUERY}]
            logger.info("test_query_only_schema_supported 完成")
        except Exception as exc:
            logger.error("test_query_only_schema_supported 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证 url/sql/path/headers 等额外参数面导致工具被跳过。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，危险 Schema 被接受时由断言报告。
    async def test_schema_with_forbidden_extra_fields_skipped(self, monkeypatch) -> None:
        """拒绝 URL/SQL/路径参数面：任何额外字段都不得进入调用参数。"""
        logger.debug("test_schema_with_forbidden_extra_fields_skipped 入口")
        try:
            # Arrange
            from pydantic import Field, create_model

            import src.search.service as service_module

            schemas = [
                create_model("WithUrl", query=(str, Field(...)), url=(str | None, Field(None))),
                create_model("WithSql", query=(str, Field(...)), sql=(str | None, Field(None))),
                create_model("WithPath", query=(str, Field(...)), path=(str | None, Field(None))),
                create_model("WithHeaders", query=(str, Field(...)), headers=(dict | None, Field(None))),
            ]

            for schema in schemas:
                tool = _make_stub_tool(_SEARCH_TOOL_NAME, schema)
                _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

                # Act
                response = await service_module.search_public_web(_QUERY)

                # Assert
                assert response.success is False, schema.__name__
                assert response.error_code == "no_tool", schema.__name__
                tool.ainvoke.assert_not_awaited()
            logger.info("test_schema_with_forbidden_extra_fields_skipped 完成")
        except Exception as exc:
            logger.error("test_schema_with_forbidden_extra_fields_skipped 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证 query 非必需、非 string、缺失 Schema 与歧义双限额字段全部跳过。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，不兼容 Schema 被调用时由断言报告。
    async def test_incompatible_schemas_skipped(self, monkeypatch) -> None:
        """Schema 缺失、歧义、不兼容均跳过，只选择唯一明确兼容形态。"""
        logger.debug("test_incompatible_schemas_skipped 入口")
        try:
            # Arrange
            from pydantic import Field, create_model

            import src.search.service as service_module

            schemas = [
                None,
                create_model("QueryOptional", query=(str, Field(None))),
                create_model("QueryNotString", query=(int, Field(...))),
                create_model(
                    "AmbiguousLimits",
                    query=(str, Field(...)),
                    max_results=(int | None, Field(None)),
                    limit=(int | None, Field(None)),
                ),
                {"type": "object", "properties": {"query": {"type": "integer"}}, "required": ["query"]},
            ]

            for schema in schemas:
                tool = _make_stub_tool(_SEARCH_TOOL_NAME, schema)
                _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

                # Act
                response = await service_module.search_public_web(_QUERY)

                # Assert
                assert response.success is False, schema
                assert response.error_code == "no_tool", schema
                tool.ainvoke.assert_not_awaited()
            logger.info("test_incompatible_schemas_skipped 完成")
        except Exception as exc:
            logger.error("test_incompatible_schemas_skipped 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证等价字典 Schema（results/items 形态）也被识别。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，拒收字典 Schema 时由断言报告。
    async def test_dict_json_schema_supported(self, monkeypatch) -> None:
        """等价字典类型说明同样按白名单解析。"""
        logger.debug("test_dict_json_schema_supported 入口")
        try:
            # Arrange
            import src.search.service as service_module

            schema = {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            }
            tool = _make_stub_tool(_SEARCH_TOOL_NAME, schema)
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            tool.ainvoke.assert_awaited_once_with({"query": _QUERY, "max_results": 8})
            logger.info("test_dict_json_schema_supported 完成")
        except Exception as exc:
            logger.error("test_dict_json_schema_supported 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证按配置顺序选择第一个兼容且可见的工具。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，选错工具时由断言报告。
    async def test_first_compatible_tool_in_config_order_selected(self, monkeypatch) -> None:
        """白名单顺序即优先级；第一个不兼容时落到下一个兼容项。"""
        logger.debug("test_first_compatible_tool_in_config_order_selected 入口")
        try:
            # Arrange
            from pydantic import Field, create_model

            import src.search.service as service_module

            incompatible = _make_stub_tool("public_search__bad", create_model("Bad", query=(int, Field(...))))
            first_ok, calls_a = _make_langchain_tool("public_search__first")
            second_ok, calls_b = _make_langchain_tool("public_search__second")
            settings = _make_settings(
                web_search_mcp_tools="public_search__bad, public_search__first, public_search__second"
            )
            _patch_service(
                monkeypatch,
                settings=settings,
                tools=[incompatible, first_ok, second_ok],
            )

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            assert len(calls_a) == 1
            assert calls_b == []
            incompatible.ainvoke.assert_not_awaited()
            logger.info("test_first_compatible_tool_in_config_order_selected 完成")
        except Exception as exc:
            logger.error("test_first_compatible_tool_in_config_order_selected 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证未加载的目标工具不可达（白名单命中名字但不在可见集合）。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，未加载工具被调用时由断言报告。
    async def test_unloaded_whitelisted_tool_not_callable(self, monkeypatch) -> None:
        """白名单为空或目标未加载时返回不可用，不为搜索建立连接。"""
        logger.debug("test_unloaded_whitelisted_tool_not_callable 入口")
        try:
            # Arrange
            import src.search.service as service_module

            _patch_service(monkeypatch, settings=_make_settings(), tools=[])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "no_tool"
            logger.info("test_unloaded_whitelisted_tool_not_callable 完成")
        except Exception as exc:
            logger.error("test_unloaded_whitelisted_tool_not_callable 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestIdentityBoundaries:
    """覆盖功能 32.1/32.2：身份漂移与跨租户可见性。"""

    # 方法作用：验证枚举工具与真正调用之间身份变化时拒绝执行。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，漂移后仍调用时由断言报告。
    async def test_identity_drift_between_listing_and_call_rejected(self, monkeypatch) -> None:
        """身份漂移（如取消后的上下文复用）不得让工具以旧身份出站。"""
        logger.debug("test_identity_drift_between_listing_and_call_rejected 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, calls = _make_langchain_tool(_SEARCH_TOOL_NAME)
            settings = _make_settings()
            identity_state = {"user_id": 7}
            manager, _ = _patch_service(monkeypatch, settings=settings, tools=[tool])
            monkeypatch.setattr(service_module, "get_current_user_id", lambda: identity_state["user_id"])

            # 方法作用：在工具枚举期间把身份切换到另一用户，模拟漂移。
            # Args: **kwargs - 服务端传入的租户与用户参数。
            # Returns: 当前可见工具列表。
            def _list_tools(**kwargs):
                identity_state["user_id"] = 8
                return [tool]

            manager.get_all_tools.side_effect = _list_tools

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "unauthorized"
            assert calls == []
            logger.info("test_identity_drift_between_listing_and_call_rejected 完成")
        except Exception as exc:
            logger.error("test_identity_drift_between_listing_and_call_rejected 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证跨租户工具通过真实 get_all_tools 过滤后不可选，同租户可选。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，越权可见时由断言报告。
    async def test_cross_tenant_tool_invisible(self, monkeypatch) -> None:
        """真实管理器过滤必须生效：他租户私有工具不构成搜索面。"""
        logger.debug("test_cross_tenant_tool_invisible 入口")
        try:
            # Arrange
            import src.api.auth as auth_module
            import src.search.service as service_module
            from src.mcp_client.client_manager import MCPClientManager

            monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")
            tool, calls = _make_langchain_tool(_SEARCH_TOOL_NAME)
            manager = MCPClientManager()
            manager.langchain_tools = {_SEARCH_TOOL_NAME: tool}
            manager._server_scopes = {"public_search": "tenant"}
            manager._server_tenants = {"public_search": 5}
            manager._server_owners = {"public_search": 0}

            settings = _make_settings()
            _patch_service(monkeypatch, settings=settings, user_id=7, tenant_id=4)
            monkeypatch.setattr(service_module, "get_mcp_client_manager", lambda *args, **kwargs: manager)

            # Act：当前租户 4 看不到租户 5 的工具
            response_other = await service_module.search_public_web(_QUERY)

            # Assert
            assert response_other.error_code == "no_tool"
            assert calls == []

            # Act：切换到租户 5 后同一工具可见
            monkeypatch.setattr(service_module, "get_current_tenant_id", lambda: 5)
            response_own = await service_module.search_public_web(_QUERY)

            # Assert
            assert response_own.success is True
            assert len(calls) == 1
            logger.info("test_cross_tenant_tool_invisible 完成")
        except Exception as exc:
            logger.error("test_cross_tenant_tool_invisible 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证服务不跨身份缓存工具列表（每次调用都按当前身份重新枚举）。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，复用旧身份结果时由断言报告。
    async def test_no_cross_identity_tool_cache(self, monkeypatch) -> None:
        """同一进程内不同用户请求不得串用上一次的工具可见性。"""
        logger.debug("test_no_cross_identity_tool_cache 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME)
            identity_state = {"user_id": 7}
            manager, _ = _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])
            monkeypatch.setattr(service_module, "get_current_user_id", lambda: identity_state["user_id"])

            # Act
            await service_module.search_public_web(_QUERY)
            identity_state["user_id"] = 8
            await service_module.search_public_web(_QUERY)

            # Assert
            assert manager.get_all_tools.call_count == 2
            assert manager.get_all_tools.call_args_list[0].kwargs == {"tenant_id": 4, "user_id": 7}
            assert manager.get_all_tools.call_args_list[1].kwargs == {"tenant_id": 4, "user_id": 8}
            logger.info("test_no_cross_identity_tool_cache 完成")
        except Exception as exc:
            logger.error("test_no_cross_identity_tool_cache 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestInvocationBudget:
    """覆盖功能 32.2：单次调用预算与失败不切换。"""

    # 方法作用：验证单次搜索请求最多一次 MCP 工具调用。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，多次调用时由断言报告。
    async def test_single_call_budget(self, monkeypatch) -> None:
        """每个搜索请求额外固定最多一次 MCP 调用。"""
        logger.debug("test_single_call_budget 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, calls = _make_langchain_tool(_SEARCH_TOOL_NAME)
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            await service_module.search_public_web(_QUERY)

            # Assert
            assert len(calls) == 1
            logger.info("test_single_call_budget 完成")
        except Exception as exc:
            logger.error("test_single_call_budget 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证首个工具失败后不切换到其他兼容工具。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，发生降级切换时由断言报告。
    async def test_failure_does_not_switch_tools(self, monkeypatch) -> None:
        """单次失败不改用其他工具，避免无界重试与隐式外送。"""
        logger.debug("test_failure_does_not_switch_tools 入口")
        try:
            # Arrange
            import src.search.service as service_module

            # 方法作用：模拟首个工具调用失败。
            # Args: kwargs - 工具参数。
            # Returns: 不返回，直接抛出运行时错误。
            async def _boom(kwargs):
                raise RuntimeError("upstream secret=abc")

            tool_a, calls_a = _make_langchain_tool("public_search__a", handler=_boom)
            tool_b, calls_b = _make_langchain_tool("public_search__b")
            settings = _make_settings(web_search_mcp_tools="public_search__a, public_search__b")
            _patch_service(monkeypatch, settings=settings, tools=[tool_a, tool_b])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "call_failed"
            assert len(calls_a) == 1
            assert calls_b == []
            logger.info("test_failure_does_not_switch_tools 完成")
        except Exception as exc:
            logger.error("test_failure_does_not_switch_tools 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestTimeoutAndCancellation:
    """覆盖功能 32.2：超时有界与取消原样传播。"""

    # 方法作用：验证工具调用超时后返回固定超时错误码且不再等待。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，超时未生效时由断言报告。
    async def test_timeout_returns_fixed_error_code(self, monkeypatch) -> None:
        """单次搜索总等待受配置上限约束。"""
        logger.debug("test_timeout_returns_fixed_error_code 入口")
        try:
            # Arrange
            import src.search.service as service_module

            # 方法作用：模拟长时间无响应的搜索工具。
            # Args: kwargs - 工具参数。
            # Returns: 超过测试超时后仍未返回。
            async def _slow(kwargs):
                await asyncio.sleep(5)
                return json.dumps({"results": []})

            tool, calls = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=_slow)
            settings = _make_settings(web_search_timeout_seconds=0.1)
            _patch_service(monkeypatch, settings=settings, tools=[tool])

            # Act
            response = await asyncio.wait_for(service_module.search_public_web(_QUERY), timeout=3)

            # Assert
            assert response.success is False
            assert response.error_code == "timeout"
            assert len(calls) == 1
            logger.info("test_timeout_returns_fixed_error_code 完成")
        except Exception as exc:
            logger.error("test_timeout_returns_fixed_error_code 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证外部取消原样传播，不被转换为失败响应。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，取消被吞时由断言报告。
    async def test_cancellation_propagates(self, monkeypatch) -> None:
        """取消必须原样传播，让上层请求生命周期正确终止。"""
        logger.debug("test_cancellation_propagates 入口")
        try:
            # Arrange
            import src.search.service as service_module

            started = asyncio.Event()

            # 方法作用：标记工具已开始执行并保持挂起等待取消。
            # Args: kwargs - 工具参数。
            # Returns: 被取消前不会返回。
            async def _hang(kwargs):
                started.set()
                await asyncio.sleep(30)
                return json.dumps({"results": []})

            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=_hang)
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            task = asyncio.create_task(service_module.search_public_web(_QUERY))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()

            # Assert
            with pytest.raises(asyncio.CancelledError):
                await task
            logger.info("test_cancellation_propagates 完成")
        except Exception as exc:
            logger.error("test_cancellation_propagates 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestBoundedResponseParsing:
    """覆盖功能 32.2：有界解析、URL 清洗与截断标记。"""

    # 方法作用：验证原始响应超限直接拒绝解析。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，超限被解析时由断言报告。
    async def test_oversized_raw_response_rejected(self, monkeypatch) -> None:
        """原始响应超过上限必须拒绝，不做部分解析。"""
        logger.debug("test_oversized_raw_response_rejected 入口")
        try:
            # Arrange
            import src.search.service as service_module

            oversized = "x" * 20_001
            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=lambda kwargs: _text(oversized))
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "response_too_large"
            logger.info("test_oversized_raw_response_rejected 完成")
        except Exception as exc:
            logger.error("test_oversized_raw_response_rejected 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证合法 JSON 结果转过为引用条目并清洗非法来源。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，条目清洗不符时由断言报告。
    async def test_results_parsed_and_dangerous_urls_dropped(self, monkeypatch) -> None:
        """只有工具实际返回且 URL 合法的条目才产生引用；重复来源去重。"""
        logger.debug("test_results_parsed_and_dangerous_urls_dropped 入口")
        try:
            # Arrange
            import src.search.service as service_module

            payload = json.dumps({
                "results": [
                    {"title": "合法一", "url": "https://example.com/a", "snippet": "摘要一"},
                    {"title": "内容摘要", "url": "https://example.com/b", "content": "兼容 content"},
                    {"title": "重复", "url": "https://example.com/a", "snippet": "重复条目"},
                    {"title": "私网", "url": "http://10.0.0.1/steal", "snippet": "私网"},
                    {"title": "凭证", "url": "http://user:pass@example.com/c", "snippet": "凭证"},
                    {"title": "脚本", "url": "javascript:alert(1)", "snippet": "脚本"},
                ]
            })
            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=lambda kwargs: _text(payload))
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            assert [hit.url for hit in response.hits] == [
                "https://example.com/a",
                "https://example.com/b",
            ]
            assert response.hits[1].snippet == "兼容 content"
            assert all(hit.provider == _SEARCH_TOOL_NAME for hit in response.hits)
            assert response.truncated is False
            logger.info("test_results_parsed_and_dangerous_urls_dropped 完成")
        except Exception as exc:
            logger.error("test_results_parsed_and_dangerous_urls_dropped 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证 items 列表与 locator.url 形式被识别。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，等价结构未识别时由断言报告。
    async def test_items_and_locator_forms_supported(self, monkeypatch) -> None:
        """兼容 items 列表与 source_id/locator.url 引用形态。"""
        logger.debug("test_items_and_locator_forms_supported 入口")
        try:
            # Arrange
            import src.search.service as service_module

            payload = json.dumps({
                "items": [
                    {"title": "定位一", "locator": {"url": "https://example.com/loc1"}, "snippet": "摘要"},
                    {"title": "定位二", "locator": "https://example.com/loc2"},
                ]
            })
            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=lambda kwargs: _text(payload))
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            assert [hit.url for hit in response.hits] == [
                "https://example.com/loc1",
                "https://example.com/loc2",
            ]
            logger.info("test_items_and_locator_forms_supported 完成")
        except Exception as exc:
            logger.error("test_items_and_locator_forms_supported 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证工具直接返回字典载荷（非 JSON 文本）时的解析。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，字典载荷被拒时由断言报告。
    async def test_dict_payload_supported(self, monkeypatch) -> None:
        """等价字典/列表载荷与 JSON 文本一样有界解析。"""
        logger.debug("test_dict_payload_supported 入口")
        try:
            # Arrange
            import src.search.service as service_module

            payload = {"results": [{"title": "字典", "url": "https://example.com/dict"}]}
            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=lambda kwargs: _dict(payload))
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            assert response.hits[0].url == "https://example.com/dict"
            logger.info("test_dict_payload_supported 完成")
        except Exception as exc:
            logger.error("test_dict_payload_supported 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证纯文本、未知结构与无有效来源返回固定失败码。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，错误码不符时由断言报告。
    async def test_unusable_payloads_return_fixed_error_codes(self, monkeypatch) -> None:
        """纯文本或无有效来源返回固定失败码；不把模型自报引用当来源。"""
        logger.debug("test_unusable_payloads_return_fixed_error_codes 入口")
        try:
            # Arrange
            import src.search.service as service_module

            cases = [
                ("这是一段没有来源的纯文本响应。", "no_usable_sources"),
                (json.dumps({"items": [{"title": "无链接"}]}), "no_usable_sources"),
                (json.dumps({"foo": "bar"}), "invalid_response"),
                ("", "invalid_response"),
            ]
            for payload, expected in cases:
                tool, _ = _make_langchain_tool(
                    _SEARCH_TOOL_NAME, handler=lambda kwargs, payload=payload: _text(payload)
                )
                _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

                # Act
                response = await service_module.search_public_web(_QUERY)

                # Assert
                assert response.success is False, payload
                assert response.error_code == expected, payload
                assert response.hits == [], payload
            logger.info("test_unusable_payloads_return_fixed_error_codes 完成")
        except Exception as exc:
            logger.error("test_unusable_payloads_return_fixed_error_codes 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证空结果正常返回 hits=[]（success=True）。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，空结果被当成失败时由断言报告。
    async def test_empty_results_is_success_with_empty_hits(self, monkeypatch) -> None:
        """空结果可正常返回，由研究层说明未检索到证据。"""
        logger.debug("test_empty_results_is_success_with_empty_hits 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME)
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            assert response.hits == []
            assert response.error_code == ""
            assert response.truncated is False
            logger.info("test_empty_results_is_success_with_empty_hits 完成")
        except Exception as exc:
            logger.error("test_empty_results_is_success_with_empty_hits 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证超过服务端条数上限时截断并标注 truncated。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，截断标记缺失时由断言报告。
    async def test_truncated_flag_when_hits_exceed_limit(self, monkeypatch) -> None:
        """结果超限截断可标注 truncated，等于上限时不标。"""
        logger.debug("test_truncated_flag_when_hits_exceed_limit 入口")
        try:
            # Arrange
            import src.search.service as service_module

            payload = _results_payload([f"https://example.com/{index}" for index in range(4)])
            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=lambda kwargs: _text(payload))
            _patch_service(monkeypatch, settings=_make_settings(web_search_max_results=2), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert len(response.hits) == 2
            assert response.truncated is True

            # Arrange：恰好等于上限
            payload = _results_payload([f"https://example.com/{index}" for index in range(2)])
            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=lambda kwargs: _text(payload))
            _patch_service(monkeypatch, settings=_make_settings(web_search_max_results=2), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert len(response.hits) == 2
            assert response.truncated is False
            logger.info("test_truncated_flag_when_hits_exceed_limit 完成")
        except Exception as exc:
            logger.error("test_truncated_flag_when_hits_exceed_limit 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestResponseTotalBudget:
    """覆盖功能 32.2：字符串/字典/列表共享统一响应预算与失败关闭。"""

    # 方法作用：验证同一内容在字符串、字典、列表三种载荷下共享同一预算边界。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；shape - 载荷形态。
    # Returns: 无返回值，预算未统一或边界不符时由断言报告。
    @pytest.mark.parametrize("shape", ["str", "dict", "list"])
    async def test_same_content_budget_for_all_supported_shapes(self, monkeypatch, shape) -> None:
        """等价 JSON 载荷与原始文本一样先过统一预算：恰等于上限放行，超一个字符整包拒绝。"""
        logger.debug("test_same_content_budget_for_all_supported_shapes 入口")
        try:
            # Arrange：同一内容的规范序列化长度即三种形态共用的预算基准。
            import src.search.service as service_module

            item = {"url": "https://example.com/report", "title": "公开资料", "snippet": "资料" * 600}
            if shape == "str":
                raw = json.dumps({"results": [item]}, ensure_ascii=False)
            elif shape == "dict":
                raw = {"results": [item]}
            else:
                raw = [item]
            canonical = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            budget = len(canonical)

            # 方法作用：按当前形态原样返回等价载荷。
            # Args: kwargs - 工具参数。
            # Returns: 当前形态的原始响应。
            async def _respond(kwargs):
                return raw

            for size, expect_success in ((budget, True), (budget - 1, False)):
                tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=_respond)
                _patch_service(
                    monkeypatch,
                    settings=_make_settings(web_search_max_response_chars=size),
                    tools=[tool],
                )

                # Act
                response = await service_module.search_public_web(_QUERY)

                # Assert
                if expect_success:
                    assert response.success is True, (shape, size)
                    assert [hit.url for hit in response.hits] == ["https://example.com/report"], shape
                else:
                    assert response.success is False, (shape, size)
                    assert response.error_code == "response_too_large", (shape, size)
                    assert response.hits == [], shape
            logger.info("test_same_content_budget_for_all_supported_shapes 完成")
        except Exception as exc:
            logger.error("test_same_content_budget_for_all_supported_shapes 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证远低于预算的小字典/小列表载荷行为不变。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；shape - 载荷形态。
    # Returns: 无返回值，小载荷被拒时由断言报告。
    @pytest.mark.parametrize("shape", ["dict", "list"])
    async def test_small_payloads_behavior_unchanged(self, monkeypatch, shape) -> None:
        """小载荷远低于默认预算，字典/列表形态继续正常产出引用与 provider。"""
        logger.debug("test_small_payloads_behavior_unchanged 入口")
        try:
            # Arrange
            import src.search.service as service_module

            item = {"title": "小载荷", "url": "https://example.com/small", "snippet": "简短摘要"}
            raw = {"results": [item]} if shape == "dict" else [item]

            # 方法作用：按当前形态原样返回等价载荷。
            # Args: kwargs - 工具参数。
            # Returns: 当前形态的原始响应。
            async def _respond(kwargs):
                return raw

            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=_respond)
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is True
            assert [hit.url for hit in response.hits] == ["https://example.com/small"]
            assert response.hits[0].provider == _SEARCH_TOOL_NAME
            assert response.truncated is False
            logger.info("test_small_payloads_behavior_unchanged 完成")
        except Exception as exc:
            logger.error("test_small_payloads_behavior_unchanged 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证巨型多条结果在构造引用前整包拒绝，不产出部分引用。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，发生部分解析时由断言报告。
    async def test_giant_multi_item_payload_rejected_before_hits(self, monkeypatch) -> None:
        """超出预算的多条合法结果必须整体拒绝，不得截断成部分引用。"""
        logger.debug("test_giant_multi_item_payload_rejected_before_hits 入口")
        try:
            # Arrange：一百条各自合法的小结果，累计远超预算。
            import src.search.service as service_module

            items = [
                {"title": f"条目{index}", "url": f"https://example.com/{index}", "snippet": "摘要" * 40}
                for index in range(100)
            ]
            tool, _ = _make_langchain_tool(
                _SEARCH_TOOL_NAME, handler=lambda kwargs: _dict({"results": items})
            )
            _patch_service(
                monkeypatch,
                settings=_make_settings(web_search_max_response_chars=2000),
                tools=[tool],
            )

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "response_too_large"
            assert response.hits == []
            assert response.truncated is False
            logger.info("test_giant_multi_item_payload_rejected_before_hits 完成")
        except Exception as exc:
            logger.error("test_giant_multi_item_payload_rejected_before_hits 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证计数在预算越界处立即停止，不触达之后的载荷尾部。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，计数越过预算继续解析尾部时由断言报告。
    async def test_budget_counting_stops_before_unserializable_tail(self, monkeypatch) -> None:
        """预算越界之后才出现的不可序列化值不得被计数触达，错误码必须是超限而非结构无效。"""
        logger.debug("test_budget_counting_stops_before_unserializable_tail 入口")
        try:
            # Arrange：首条即超预算，尾部放不可序列化值用于探测是否被触达。
            import src.search.service as service_module

            tail = [
                {"url": "https://example.com/big", "title": "大条目", "snippet": "摘要" * 2500},
                object(),
            ]

            # 方法作用：返回首条超预算、尾部不可序列化的列表载荷。
            # Args: kwargs - 工具参数。
            # Returns: 构造好的列表载荷。
            async def _respond(kwargs):
                return tail

            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=_respond)
            _patch_service(
                monkeypatch,
                settings=_make_settings(web_search_max_response_chars=2000),
                tools=[tool],
            )

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "response_too_large"
            assert response.hits == []
            logger.info("test_budget_counting_stops_before_unserializable_tail 完成")
        except Exception as exc:
            logger.error("test_budget_counting_stops_before_unserializable_tail 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证含非 JSON 类型值的等价载荷按固定失败码关闭。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具；bad_value - 不可序列化的值。
    # Returns: 无返回值，非 JSON 值被静默忽略后部分成功时由断言报告。
    @pytest.mark.parametrize("bad_value", [object(), b"raw-bytes", {1, 2}])
    async def test_unserializable_payload_fails_closed(self, monkeypatch, bad_value) -> None:
        """载荷含不可序列化值时无法纳入字符预算，必须固定失败码拒绝而不是部分成功。"""
        logger.debug("test_unserializable_payload_fails_closed 入口")
        try:
            # Arrange
            import src.search.service as service_module

            payload = {
                "results": [
                    {"title": "混合", "url": "https://example.com/mixed", "snippet": bad_value}
                ]
            }
            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=lambda kwargs: _dict(payload))
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "invalid_response"
            assert response.hits == []
            logger.info("test_unserializable_payload_fails_closed 完成")
        except Exception as exc:
            logger.error("test_unserializable_payload_fails_closed 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证循环引用结构按固定失败码关闭，不进入无界遍历。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，循环结构被接受或遍历时由断言报告。
    async def test_circular_payload_fails_closed(self, monkeypatch) -> None:
        """循环引用无法有界计数与遍历，必须固定失败码拒绝。"""
        logger.debug("test_circular_payload_fails_closed 入口")
        try:
            # Arrange
            import src.search.service as service_module

            circular_item: dict = {"title": "循环", "url": "https://example.com/loop"}
            circular_item["related"] = [circular_item]
            tool, _ = _make_langchain_tool(
                _SEARCH_TOOL_NAME, handler=lambda kwargs: _dict({"results": [circular_item]})
            )
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])
            fake_logger = MagicMock()
            monkeypatch.setattr(service_module, "logger", fake_logger)

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert：固定失败码 + 异常边界一次 error 保留原始堆栈 + 全程不泄露载荷原文。
            assert response.success is False
            assert response.error_code == "invalid_response"
            assert response.hits == []
            assert fake_logger.error.call_count == 1
            exc_info = fake_logger.error.call_args.kwargs.get("exc_info")
            assert isinstance(exc_info, tuple) and len(exc_info) == 3
            assert exc_info[2] is not None, "必须保留原始 traceback"
            _assert_logs_are_clean(fake_logger, "https://example.com/loop", "循环")
            logger.info("test_circular_payload_fails_closed 完成")
        except Exception as exc:
            logger.error("test_circular_payload_fails_closed 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestLoggingBoundaries:
    """覆盖功能 32.2：日志脱敏与高频成功路径无噪声。"""

    # 方法作用：验证密集成功调用不产生 INFO 及以上级别日志。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，出现成功噪声时由断言报告。
    async def test_dense_successful_calls_emit_no_info_noise(self, monkeypatch) -> None:
        """正常密集调用不得新增成功噪声（仅 debug 计数/耗时）。"""
        logger.debug("test_dense_successful_calls_emit_no_info_noise 入口")
        try:
            # Arrange
            import src.search.service as service_module

            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME)
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])
            fake_logger = MagicMock()
            monkeypatch.setattr(service_module, "logger", fake_logger)

            # Act
            for _ in range(5):
                response = await service_module.search_public_web(_QUERY)
                assert response.success is True

            # Assert
            assert fake_logger.info.call_count == 0
            assert fake_logger.warning.call_count == 0
            assert fake_logger.error.call_count == 0
            assert fake_logger.debug.call_count >= 5
            logger.info("test_dense_successful_calls_emit_no_info_noise 完成")
        except Exception as exc:
            logger.error("test_dense_successful_calls_emit_no_info_noise 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证失败日志只含固定错误码与计数字段，不含问法原文与响应正文。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，日志泄漏时由断言报告。
    async def test_failure_logs_fixed_error_code_without_sensitive_text(self, monkeypatch) -> None:
        """日志仅含身份、工具名、字符数、耗时、条目数和固定错误码。"""
        logger.debug("test_failure_logs_fixed_error_code_without_sensitive_text 入口")
        try:
            # Arrange
            import src.search.service as service_module

            secret_body = "机密正文内容-XYZ"
            tool, _ = _make_langchain_tool(
                _SEARCH_TOOL_NAME,
                handler=lambda kwargs: _text(secret_body),
            )
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])
            fake_logger = MagicMock()
            monkeypatch.setattr(service_module, "logger", fake_logger)

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.error_code == "no_usable_sources"
            assert fake_logger.info.call_count >= 1
            assert any(
                call.kwargs.get("error_code") == "no_usable_sources"
                for call in fake_logger.info.call_args_list
            )
            _assert_logs_are_clean(fake_logger, _QUERY, secret_body, "受控联网测试主题")
            logger.info("test_failure_logs_fixed_error_code_without_sensitive_text 完成")
        except Exception as exc:
            logger.error("test_failure_logs_fixed_error_code_without_sensitive_text 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证工具异常时使用脱敏异常与原始 traceback，且不泄漏底层异常文本。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，异常原文泄漏时由断言报告。
    async def test_call_failure_logs_sanitized_exception_with_raw_traceback(self, monkeypatch) -> None:
        """异常边界一次 error(exc_info)，用脱敏异常对象保留原堆栈。"""
        logger.debug("test_call_failure_logs_sanitized_exception_with_raw_traceback 入口")
        try:
            # Arrange
            import src.search.service as service_module

            # 方法作用：模拟携带凭证的底层工具异常。
            # Args: kwargs - 工具参数。
            # Returns: 不返回，直接抛出带敏感文本的异常。
            async def _boom(kwargs):
                raise RuntimeError("Authorization: Bearer super-secret-token")

            tool, _ = _make_langchain_tool(_SEARCH_TOOL_NAME, handler=_boom)
            _patch_service(monkeypatch, settings=_make_settings(), tools=[tool])
            fake_logger = MagicMock()
            monkeypatch.setattr(service_module, "logger", fake_logger)

            # Act
            response = await service_module.search_public_web(_QUERY)

            # Assert
            assert response.success is False
            assert response.error_code == "call_failed"
            assert fake_logger.error.call_count == 1
            error_kwargs = fake_logger.error.call_args.kwargs
            exc_info = error_kwargs.get("exc_info")
            assert exc_info is not None, "异常边界缺少 exc_info"
            assert isinstance(exc_info, tuple) and len(exc_info) == 3
            assert "super-secret-token" not in str(exc_info[1])
            assert exc_info[2] is not None, "必须保留原始 traceback"
            _assert_logs_are_clean(fake_logger, _QUERY, "super-secret-token")
            logger.info("test_call_failure_logs_sanitized_exception_with_raw_traceback 完成")
        except Exception as exc:
            logger.error("test_call_failure_logs_sanitized_exception_with_raw_traceback 异常", extra={"error": str(exc)}, exc_info=True)
            raise
