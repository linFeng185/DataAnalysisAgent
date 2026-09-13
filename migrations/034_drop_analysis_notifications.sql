-- 删除旧站内通知表（需求 1.13）。编号核对：033 为本模块上一条迁移，034 未被占用。
--
-- 背景：旧通知能力只服务「自动化」任务，随「自动化」菜单与
-- GET /automation/notifications 接口一并下线；项目未上线，历史数据无需保留。
-- 通用通知模块改用 033 的 notifications / notification_recipients。
--
-- 影响面核对（已逐处同步修改，勿再回退）：
--   * src/automation/store.py：删除 record_success 的通知写入与 list_notifications。
--   * src/api/routes/automation.py：删除 GET /automation/notifications 路由。
--   * src/tenant_purge.py：租户清理改为清理 033 的三张通知表。
--   * migrations/009_automation.sql 与 010_automation_force_rls.sql 保留原文，
--     作为历史迁移记录；本文件之后 analysis_notifications 不再存在。
--   * 基线证据：删除前 `pytest tests/test_automation -q` 为 23 passed，
--     删除并同步更新用例后仍为全部通过（见验收报告）。

DROP TABLE IF EXISTS analysis_notifications CASCADE;
