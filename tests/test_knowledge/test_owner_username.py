"""知识条目与文档创建人用户名测试，覆盖 spec/44 §2.3。

事实依据：pytest 经 tests/test_knowledge/__init__.py 收集执行；同目录既有 28 个
测试文件均不覆盖创建人映射主题；本文件不读写任何数据文件，只断言纯函数行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api.routes.knowledge import _normalize_owner_username, _resolve_owner_user_id  # noqa: E402


class TestOwnerUsername:
    """验证创建人映射的边界行为。"""

    # 方法作用：验证无主条目映射为空串而不是报错。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_zero_owner_maps_to_empty(self) -> None:
        """owner_user_id=0 表示系统写入，前端显示「系统」。"""
        # Act / Assert
        assert _normalize_owner_username(0, {}) == ""

    # 方法作用：验证映射命中时返回用户名。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_matched_owner_returns_username(self) -> None:
        """命中 users 表时返回真实用户名。"""
        # Act / Assert
        assert _normalize_owner_username(3, {3: "test2"}) == "test2"

    # 方法作用：验证用户被删除后不抛异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_user_returns_empty(self) -> None:
        """用户已注销时保持列表可用。"""
        # Act / Assert
        assert _normalize_owner_username(99, {3: "test2"}) == ""

    # 方法作用：验证空值 owner 不抛异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_none_owner_returns_empty(self) -> None:
        """metadata 缺少 owner_user_id 时必须安全降级。"""
        # Act / Assert
        assert _normalize_owner_username(None, {}) == ""

    # 方法作用：验证字符串形式的 owner 也能映射。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_string_owner_is_coerced(self) -> None:
        """部分驱动会把整数 metadata 还原成字符串。"""
        # Act / Assert
        assert _normalize_owner_username("3", {3: "test2"}) == "test2"

    # 方法作用：验证非法 owner 值不抛异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalid_owner_degrades_to_empty(self) -> None:
        """脏数据不得让整个列表 500。"""
        # Act / Assert
        assert _normalize_owner_username("abc", {3: "test2"}) == ""


class TestResolveOwnerUserId:
    """验证创建人 ID 解析对 uploaded_by_user_id 键的兼容。"""

    # 方法作用：验证 tenant 范围条目回退读取 uploaded_by_user_id。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tenant_scope_falls_back_to_uploaded_by(self) -> None:
        """上传者字段存在但键名不同，必须回退读取，否则创建人恒为空。"""
        # Act / Assert
        assert _resolve_owner_user_id({"uploaded_by_user_id": 7}) == 7

    # 方法作用：验证两键并存时以 owner_user_id 为准。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_owner_user_id_takes_priority(self) -> None:
        """私有范围条目以 owner_user_id 为权威。"""
        # Act / Assert
        assert _resolve_owner_user_id({"owner_user_id": 3, "uploaded_by_user_id": 7}) == 3

    # 方法作用：验证 owner_user_id=0 时补全为上传者。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_zero_owner_falls_back(self) -> None:
        """零值等同缺失，需要回退而非显示为无主。"""
        # Act / Assert
        assert _resolve_owner_user_id({"owner_user_id": 0, "uploaded_by_user_id": 7}) == 7

    # 方法作用：验证两个键都缺失时返回 None。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_both_returns_none(self) -> None:
        """无主条目保持 None，前端显示「系统」。"""
        # Act / Assert
        assert _resolve_owner_user_id({}) is None
