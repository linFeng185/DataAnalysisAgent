"""知识条目列表的循环热点回归测试，覆盖 spec/44 §4.2。

事实依据：pytest 经 tests/conftest.py 与 tests/test_knowledge/__init__.py 收集；
同目录无性能热点主题文件；本文件不读写数据文件，只做纯函数断言。

断言授权判定调用次数只与「可见范围组合数」相关，不随条目数线性增长。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestAuthorizationCallCount:
    """验证同组合条目共用一次授权判定。"""

    # 方法作用：验证 6000 条同组合条目只触发一次授权判定。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_same_combination_judged_once(self) -> None:
        """组合缓存必须消除逐条授权开销。"""
        # Arrange
        calls: list[tuple] = []

        def fake_can_manage(scope, *, role, current_tenant_id, resource_tenant_id,
                            current_user_id, owner_user_id):
            calls.append((scope, resource_tenant_id, owner_user_id))
            return True

        from src.api.routes.knowledge import _judge_delete_permission

        entries = [
            {"scope": "tenant", "tenant_id": 3, "owner_user_id": 0, "source": "user_upload"}
            for _ in range(6000)
        ]

        # Act
        _judge_delete_permission(
            entries,
            role="tenant_admin",
            current_tenant_id=3,
            current_user_id=3,
            can_manage=fake_can_manage,
        )

        # Assert
        assert len(calls) == 1
        assert all(entry["can_delete"] is True for entry in entries)

    # 方法作用：验证不同组合仍各自判定。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_distinct_combinations_judged_separately(self) -> None:
        """不同属主必须分别判定，缓存不得越权。"""
        # Arrange
        calls: list[tuple] = []

        def fake_can_manage(scope, *, role, current_tenant_id, resource_tenant_id,
                            current_user_id, owner_user_id):
            calls.append((scope, resource_tenant_id, owner_user_id))
            return owner_user_id == 3

        from src.api.routes.knowledge import _judge_delete_permission

        entries = [
            {"scope": "private", "tenant_id": 3, "owner_user_id": 3, "source": "user_upload"},
            {"scope": "private", "tenant_id": 3, "owner_user_id": 7, "source": "user_upload"},
        ]

        # Act
        _judge_delete_permission(
            entries,
            role="analyst",
            current_tenant_id=3,
            current_user_id=3,
            can_manage=fake_can_manage,
        )

        # Assert
        assert len(calls) == 2
        assert entries[0]["can_delete"] is True
        assert entries[1]["can_delete"] is False

    # 方法作用：验证非用户上传条目直接判定为不可删。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_user_upload_never_deletable(self) -> None:
        """内省与目录扫描条目不得走授权函数。"""
        # Arrange
        calls: list[str] = []

        def fake_can_manage(scope, **kwargs):
            calls.append(scope)
            return True

        from src.api.routes.knowledge import _judge_delete_permission

        entries = [
            {"scope": "tenant", "tenant_id": 3, "owner_user_id": 0, "source": "auto_introspect"},
        ]

        # Act
        _judge_delete_permission(
            entries, role="tenant_admin", current_tenant_id=3, current_user_id=3,
            can_manage=fake_can_manage,
        )

        # Assert
        assert calls == []
        assert entries[0]["can_delete"] is False

    # 方法作用：验证组合缓存不跨 range 串号。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_combination_cache_key_covers_scope_and_owner(self) -> None:
        """同租户不同 visibility 必须区分判定。"""
        # Arrange
        seen: list[tuple] = []

        def fake_can_manage(scope, *, role, current_tenant_id, resource_tenant_id,
                            current_user_id, owner_user_id):
            seen.append((scope, resource_tenant_id, owner_user_id))
            return scope == "tenant"

        from src.api.routes.knowledge import _judge_delete_permission

        entries = [
            {"scope": "tenant", "tenant_id": 3, "owner_user_id": 0, "source": "user_upload"},
            {"scope": "private", "tenant_id": 3, "owner_user_id": 3, "source": "user_upload"},
        ]

        # Act
        _judge_delete_permission(
            entries, role="tenant_admin", current_tenant_id=3, current_user_id=3,
            can_manage=fake_can_manage,
        )

        # Assert
        assert len(seen) == 2
        assert entries[0]["can_delete"] is True
        assert entries[1]["can_delete"] is False
