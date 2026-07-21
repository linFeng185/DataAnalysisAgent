"""公开注册接口限流回归测试。"""

from __future__ import annotations

import time


class TestRegistrationRateLimit:
    """覆盖注册端点按客户端地址的滑动窗口限制。"""

    # 验证同一客户端超过窗口上限后被拒绝，过期记录可以回收。
    # Args: self - 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_registration_limit_rejects_burst_and_cleans_stale(self) -> None:
        import src.api.auth as auth_module

        auth_module._registration_limits.clear()  # noqa: SLF001
        assert auth_module._check_registration_rate_limit("198.51.100.10", 2) is True  # noqa: SLF001
        assert auth_module._check_registration_rate_limit("198.51.100.10", 2) is True  # noqa: SLF001
        assert auth_module._check_registration_rate_limit("198.51.100.10", 2) is False  # noqa: SLF001
        auth_module._registration_limits["stale"] = [time.monotonic() - 3601]  # noqa: SLF001
        auth_module._check_registration_rate_limit("198.51.100.11", 2)  # noqa: SLF001
        assert "stale" not in auth_module._registration_limits  # noqa: SLF001


class TestLoginRateLimit:
    """覆盖登录端点按客户端与账号组合执行滑动窗口限制。"""

    # 方法作用：验证同一来源对同一账号的连续登录尝试会被限制。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_login_limit_rejects_repeated_attempts(self) -> None:
        """登录限流键同时包含客户端和规范化用户名。"""
        # Arrange
        import src.api.auth as auth_module

        auth_module._login_limits.clear()  # noqa: SLF001

        # Act / Assert
        assert auth_module._check_login_rate_limit("198.51.100.10", "Alice", 2) is True  # noqa: SLF001
        assert auth_module._check_login_rate_limit("198.51.100.10", "alice", 2) is True  # noqa: SLF001
        assert auth_module._check_login_rate_limit("198.51.100.10", "ALICE", 2) is False  # noqa: SLF001
        assert auth_module._check_login_rate_limit("198.51.100.11", "alice", 2) is True  # noqa: SLF001
