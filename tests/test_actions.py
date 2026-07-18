"""受控外部动作接口测试。"""

from __future__ import annotations

import pytest


class TestExternalActions:
    """覆盖人工确认、幂等键和默认拒绝。"""

    async def test_dispatch_requires_confirmation_and_idempotency(self):
        """动作未确认或缺少幂等键时不得执行。"""
        # Arrange
        from src.actions.contracts import ActionRequest, ExternalActionRegistry

        registry = ExternalActionRegistry()

        # Act
        pending = await registry.dispatch(ActionRequest("notify", {"text": "x"}, "key-1"))
        missing = await registry.dispatch(ActionRequest("notify", {"text": "x"}, "", confirmed=True))

        # Assert
        assert pending.status == "confirmation_required"
        assert missing.status == "rejected"

    async def test_dispatch_runs_registered_action_once_by_idempotency_key(self):
        """已确认动作同一幂等键只能执行一次。"""
        # Arrange
        from src.actions.contracts import ActionRequest, ExternalAction, ExternalActionRegistry

        class NotifyAction(ExternalAction):
            name = "notify"

            async def execute(self, request):
                return {"sent": request.payload["text"]}

        registry = ExternalActionRegistry()
        registry.register(NotifyAction())
        request = ActionRequest("notify", {"text": "x"}, "key-1", confirmed=True)

        # Act
        first = await registry.dispatch(request)
        second = await registry.dispatch(request)

        # Assert
        assert first.status == "executed"
        assert second.status == "already_executed"

    async def test_unregistered_action_is_rejected_and_audited(self):
        """确认后的未注册动作必须拒绝，并留下审计记录。"""
        # Arrange
        from src.actions.contracts import ActionRequest, ExternalActionRegistry

        registry = ExternalActionRegistry()

        # Act
        result = await registry.dispatch(ActionRequest("email", {}, "key-2", confirmed=True))

        # Assert
        assert result.status == "rejected"
        assert registry.audit_log()[-1]["status"] == "rejected"

    async def test_trading_action_is_disabled_by_default(self):
        """即使动作被注册，交易类动作也不得自动执行。"""
        # Arrange
        from src.actions.contracts import ActionRequest, ExternalAction, ExternalActionRegistry

        class BuyAction(ExternalAction):
            name = "buy"

            async def execute(self, request):
                return {"executed": True}

        registry = ExternalActionRegistry()
        registry.register(BuyAction())

        # Act
        result = await registry.dispatch(ActionRequest("buy", {"symbol": "000001.SZ"}, "key-3", confirmed=True))

        # Assert
        assert result.status == "rejected"
        assert "交易" in result.message
