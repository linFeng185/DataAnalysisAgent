"""通知迁移契约测试：033 建表 / RLS / 默认渠道，034 删除旧表。"""

from __future__ import annotations

from pathlib import Path

_MIGRATION = Path("migrations/033_notifications.sql").read_text(encoding="utf-8")
_DROP_MIGRATION = Path("migrations/034_drop_analysis_notifications.sql").read_text(encoding="utf-8")


class TestNotificationMigration:
    """覆盖 033 / 034 的数据库契约。"""

    # 方法作用：验证三张通知表都启用并强制 RLS。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_tables_force_row_level_security(self) -> None:
        """仅 ENABLE RLS 不能阻止表所有者绕过租户策略。"""
        # Arrange / Act / Assert
        for table in ("notifications", "notification_recipients", "notification_channels"):
            assert "ALTER TABLE %I ENABLE ROW LEVEL SECURITY" in _MIGRATION
            assert "FORCE ROW LEVEL SECURITY" in _MIGRATION
            assert f"CREATE POLICY {table}_scope" in _MIGRATION or f"{table}_scope ON {table}" in _MIGRATION

    # 方法作用：验证站内信默认启用是迁移层写入的默认值。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_in_app_channel_is_seeded_enabled(self) -> None:
        """in_app 的默认行必须 enabled=TRUE，且不覆盖已有配置。"""
        # Arrange / Act / Assert
        assert "'in_app', TRUE" in _MIGRATION
        assert "ON CONFLICT (tenant_id, channel) DO NOTHING" in _MIGRATION

    # 方法作用：验证收件箱索引覆盖租户 + 用户 + 时间倒序。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_inbox_index_supports_unread_queries(self) -> None:
        """未读计数与收件箱分页依赖 (tenant_id, user_id, created_at DESC)。"""
        # Arrange / Act / Assert
        assert (
            "idx_notification_recipients_inbox\n    ON notification_recipients "
            "(tenant_id, user_id, created_at DESC)" in _MIGRATION
        )

    # 方法作用：验证 kind 与 channel 的枚举约束完整。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_enum_check_constraints(self) -> None:
        """四类通知与四个渠道都必须在 CHECK 约束里。"""
        # Arrange / Act / Assert
        assert "kind IN ('platform','tenant','group','personal')" in _MIGRATION
        assert "channel IN ('in_app','email','feishu','slack')" in _MIGRATION

    # 方法作用：验证 034 删除旧表并说明影响面。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_drop_migration_removes_legacy_table(self) -> None:
        """旧 analysis_notifications 必须被删除，并注明同步改动点。"""
        # Arrange / Act / Assert
        assert "DROP TABLE IF EXISTS analysis_notifications CASCADE;" in _DROP_MIGRATION
        assert "src/tenant_purge.py" in _DROP_MIGRATION
        assert "src/automation/store.py" in _DROP_MIGRATION
