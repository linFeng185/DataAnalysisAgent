"""API 路由兼容调用辅助测试。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
class TestRouteHelpers:
    """覆盖租户参数兼容调用的正常、旧签名和异常路径。"""

    # 方法作用：验证未知 callable 签名时不会重试方法体内部的 TypeError。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unknown_signature_does_not_retry_internal_type_error(self) -> None:
        """异步方法体异常必须原样传播且只执行一次。"""
        # Arrange
        from src.api.routes._helpers import _call_with_optional_tenant

        class _OpaqueCallable:
            __signature__ = object()

            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, **_kwargs):
                self.calls += 1
                raise TypeError("unexpected keyword argument from method body")

        method = _OpaqueCallable()

        # Act / Assert
        with pytest.raises(TypeError, match="from method body"):
            await _call_with_optional_tenant(method, tenant_id=7)
        assert method.calls == 1

    # 方法作用：验证没有租户参数的旧方法仍走无参数兼容调用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_legacy_signature_is_called_without_tenant(self) -> None:
        """旧版 list_all 签名不得收到未知关键字。"""
        # Arrange
        from src.api.routes._helpers import _call_with_optional_tenant

        calls: list[str] = []

        async def _legacy() -> list[str]:
            calls.append("called")
            return ["sales"]

        # Act
        result = await _call_with_optional_tenant(_legacy, tenant_id=7)

        # Assert
        assert result == ["sales"]
        assert calls == ["called"]

    # 方法作用：验证 positional-only 租户参数按位置传递而不是错误使用关键字。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_positional_only_tenant_is_supported(self) -> None:
        """特殊 Provider 签名仍应收到当前租户。"""
        # Arrange
        from src.api.routes._helpers import _call_with_optional_tenant

        async def _positional_only(_name: str, tenant_id: int | None = None, /):
            return tenant_id

        # Act
        result = await _call_with_optional_tenant(_positional_only, "sales", tenant_id=7)

        # Assert
        assert result == 7
