"""G24.1/G24.3/G24.4 RLS 谓词契约，不替代 PostgreSQL 集成验收。"""

import re
import sqlite3
from pathlib import Path

import pytest


# 将 SQL 稳定函数的标量谓词置入 SQLite 验证三值逻辑。
# Args: name - 函数名；role/tenant/user - 当前身份；arguments - 资源字段。
# Returns: 谓词计算结果。
def evaluate_policy(name, role, tenant, user, **arguments):
    source = Path("migrations/024_platform_tenant_boundaries.sql").read_text(encoding="utf-8")
    match = re.search(rf"FUNCTION {name}\(.*?AS \$\$(.*?)\$\$", source, re.S)
    sql = match[1]
    for key, value in arguments.items():
        sql = re.sub(rf"\b{key}\b", f"'{value}'" if isinstance(value, str) else str(value), sql)
    sql = re.sub(r"(NULLIF\(current_setting\('[^']+',true\),''\))::bigint", r"CAST(\1 AS INTEGER)", sql)
    with sqlite3.connect(":memory:") as connection:
        settings = {"app.current_role": role, "app.current_tenant_id": str(tenant), "app.current_user_id": str(user)}
        connection.create_function("current_setting", 2, lambda key, missing: settings.get(key))
        connection.create_function("app_is_background", 0, lambda: role == "super_admin" and tenant == 0 and user == 0)
        return bool(connection.execute(sql).fetchone()[0])


class TestPlatformGovernanceMigration:
    """验证超管日常身份与系统后台例外严格分离。"""

    # 验证报表 RLS 不会因旧超管角色放行。
    # Args: self - 测试实例；role/tenant/user/allowed - 身份矩阵。
    # Returns: 无返回值。
    @pytest.mark.parametrize("role,tenant,user,allowed", [
        ("super_admin", 1, 1, False), ("super_admin", 2, 7, False),
        ("super_admin", 0, 0, True), ("tenant_admin", 2, 8, True),
        ("tenant_admin", 3, 8, False), ("analyst", 2, 7, True), ("analyst", 2, 8, False),
    ])
    def test_report_rls_matrix(self, role, tenant, user, allowed) -> None:
        """只能后台或当前租户业务拥有者/管理员访问。"""
        assert evaluate_policy("app_report_identity", role, tenant, user, resource_tenant=2, resource_owner=7) == allowed

    # 验证私有资源不可被租户管理员或超管默认读取/管理。
    # Args: self - 测试实例；name - 读取或写入谓词。
    # Returns: 无返回值。
    @pytest.mark.parametrize("name", ["app_scope_read", "app_scope_manage"])
    def test_private_scope_matrix(self, name) -> None:
        """知识与 MCP 共用相同的私有归属约束。"""
        arguments = {"resource_scope": "private", "resource_tenant": 2, "resource_owner": 7}
        assert not evaluate_policy(name, "super_admin", 2, 7, **arguments)
        assert not evaluate_policy(name, "tenant_admin", 2, 8, **arguments)
        assert evaluate_policy(name, "analyst", 2, 7, **arguments)

    # 验证所有关键表已替换旧角色绕过分支，分享策略不误删。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_policy_upgrade_contract(self) -> None:
        """迁移追加新版本，不修改 018/022；授权只读例外单独命名。"""
        source = Path("migrations/024_platform_tenant_boundaries.sql").read_text(encoding="utf-8")
        assert "knowledge_files_emergency_read" in source
        assert "grant_row.revoked_at IS NULL" in source
        assert "grant_row.expires_at>clock_timestamp()" in source
        assert "DROP POLICY IF EXISTS report_versions_share_read" not in source
        assert "CREATE POLICY report_live_queries_identity" in source
