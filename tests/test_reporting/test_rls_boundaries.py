"""报表 RLS 策略布尔边界测试，不连接用户数据库。"""

import re
import sqlite3
from pathlib import Path

import pytest

from src.logging_config import get_logger

logger = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[2]


# 方法作用：提取迁移中的 USING 条件，用标量 SQL 复现身份谓词而不访问业务记录。
# Args: filename - 迁移文件；policy - 策略名；tenant_id - 记录租户；role - 当前角色。
# Returns: 条件是否允许访问。
def evaluate_policy(filename: str, policy: str, tenant_id: int, role: str) -> bool:
    """保留数值转换语义，隔离验证 PostgreSQL 策略的布尔条件。"""
    text = (ROOT / "migrations" / filename).read_text(encoding="utf-8")
    match = re.search(rf"CREATE POLICY {policy}\b.*?USING\s*\(", text, re.S)
    assert match is not None
    start = match.end()
    depth = 1
    end = start
    while depth:
        depth += (text[end] == "(") - (text[end] == ")")
        end += 1
    expression = text[start:end - 1]
    for name, value in {"current_role": role, "current_tenant_id": "3", "current_user_id": "7", "report_share_access": "false"}.items():
        expression = re.sub(rf"current_setting\('app\.{name}',\s*true\)", f"'{value}'", expression)
    expression = re.sub(r"(NULLIF\([^)]*\))::bigint", r"CAST(\1 AS INTEGER)", expression, flags=re.I)
    with sqlite3.connect(":memory:") as connection:
        probe = connection.execute("SELECT 3 = NULLIF('3',''), 3 = CAST(NULLIF('3','') AS INTEGER)").fetchone()
        logger.info("RLS 方言标量类型探针", uncast_equal=probe[0], cast_equal=probe[1])
        result = connection.execute(f"SELECT ({expression}) FROM (SELECT ? AS tenant_id, 7 AS user_id, 7 AS created_by)", (tenant_id,)).fetchone()[0]
    logger.info("RLS 标量谓词探针", migration=filename, policy=policy, row_tenant=tenant_id, identity_tenant=3, role=role, allowed=bool(result))
    return bool(result)


class TestReportRlsBoundaries:
    """覆盖 23.2、23.8 的数据库租户边界。"""

    # 方法作用：固定记录旧迁移中不受租户限制的管理员分支，保留根因证据。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_original_policy_reproduces_cross_tenant_predicate(self) -> None:
        """旧策略仅凭 tenant_admin 角色就会对其他租户返回真。"""
        assert evaluate_policy("018_report_workbench.sql", "report_quota_policies_identity", 99, "tenant_admin")

    # 方法作用：验证修正后的策略仅在租户绑定成立时赋予管理员业务权限。
    # Args: self - 测试实例；policy - 报表策略名称。
    # Returns: 无返回值。
    @pytest.mark.parametrize("policy", ["report_jobs_identity", "report_runs_identity", "report_shares_identity", "report_user_permissions_identity", "report_quota_policies_identity", "report_quota_usage_identity", "report_access_audit_identity"])
    def test_corrected_policy_requires_tenant_binding(self, policy: str) -> None:
        """同租户管理员可用，跨租户管理员及普通用户均被拒绝。"""
        assert evaluate_policy("022_report_tenant_boundaries.sql", policy, 3, "tenant_admin")
        assert not evaluate_policy("022_report_tenant_boundaries.sql", policy, 99, "tenant_admin")
        assert not evaluate_policy("022_report_tenant_boundaries.sql", policy, 99, "analyst")
