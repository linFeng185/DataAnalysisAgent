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

    # 方法作用：验证审计记录使用有界缓冲而不会无限增长。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_audit_log_is_bounded(self):
        """高频拒绝请求只能保留配置数量的最新审计。"""
        from src.actions.contracts import ActionRequest, ExternalActionRegistry

        registry = ExternalActionRegistry(audit_max_entries=2)
        for index in range(3):
            await registry.dispatch(ActionRequest("missing", {}, str(index), confirmed=True))

        assert len(registry.audit_log()) == 2

    # 方法作用：验证幂等记录达到上限后拒绝新动作且保留既有键。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_idempotency_capacity_rejects_new_execution(self):
        """有界幂等表不得通过淘汰旧键造成动作重复执行。"""
        from src.actions.contracts import ActionRequest, ExternalAction, ExternalActionRegistry

        class Action(ExternalAction):
            name = "notify"

            async def execute(self, request):
                return request.payload

        registry = ExternalActionRegistry(max_idempotency_keys=1)
        registry.register(Action())
        first = ActionRequest("notify", {"value": 1}, "key-1", confirmed=True)
        second = ActionRequest("notify", {"value": 2}, "key-2", confirmed=True)

        assert (await registry.dispatch(first)).status == "executed"
        result = await registry.dispatch(second)

        assert result.status == "rejected"
        assert "容量" in result.message
        assert (await registry.dispatch(first)).status == "already_executed"
