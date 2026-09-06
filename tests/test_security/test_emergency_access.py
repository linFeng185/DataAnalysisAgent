"""G24.3 显式知识应急授权的边界测试。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


class TestEmergencyAccess:
    """覆盖文件级授权、到期、撤销、归属变化与角色隔离。"""

    # 验证授权不可由平台自批，也不能由租户管理员代批个人知识。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_grant_authority(self) -> None:
        """私有文件仅拥有者可授权，tenant 文件仅租户管理员可授权。"""
        from src.security.emergency_access import can_grant_file

        private = {"knowledge_scope": "private", "tenant_id": 2, "user_id": 7}
        assert can_grant_file(private, 2, 7, "analyst")
        assert not can_grant_file(private, 2, 8, "tenant_admin")
        assert not can_grant_file(private, 2, 7, "super_admin")
        assert not can_grant_file(private, 3, 7, "analyst")
        assert not can_grant_file({**private, "knowledge_scope": "system"}, 2, 7, "super_admin")

    # 验证只读授权每次读取都检查有效时间、受让人和文件归属。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_grant_validity(self) -> None:
        """过期、撤销、文件换主和非受让者均不得读取。"""
        from src.security.emergency_access import validate_emergency_grant

        now = datetime.now(timezone.utc)
        row = {"grantee_user_id": 1, "revoked_at": None, "expires_at": now + timedelta(minutes=1),
               "scope": "private", "tenant_id": 2, "owner_user_id": 7, "issued_by": 7,
               "issuer_role": "analyst", "issuer_active": True, "tenant_active": True}
        file = {"knowledge_scope": "private", "tenant_id": 2, "user_id": 7}
        validate_emergency_grant(row, file, user_id=1, role="super_admin", now=now)
        for changed in ({"revoked_at": now}, {"expires_at": now}, {"grantee_user_id": 3}, {"issuer_active": False}):
            with pytest.raises(HTTPException):
                validate_emergency_grant({**row, **changed}, file, user_id=1, role="super_admin", now=now)
        with pytest.raises(HTTPException):
            validate_emergency_grant(row, {**file, "user_id": 9}, user_id=1, role="super_admin", now=now)

    # 验证空理由、越界期限和非法 ID 被请求模型阻断。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_grant_input_bounds(self) -> None:
        """最长授权 60 分钟且必须说明理由。"""
        from src.security.emergency_access import EmergencyGrantRequest

        for values in ({"minutes": 61}, {"reason": " "}, {"file_id": 0}):
            with pytest.raises(ValidationError):
                EmergencyGrantRequest(**({"file_id": 1, "grantee_user_id": 1, "minutes": 15, "reason": "排查工单"} | values))
