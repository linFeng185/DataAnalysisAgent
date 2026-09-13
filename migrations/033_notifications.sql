-- 通用通知模块（需求 1.10 / 1.12 / 1.13）。编号核对：031 为上一条迁移，033 未被占用。
-- SQLite 测试不执行本文件；NotificationStore(memory=True) 用应用层字典模拟这三张表。
--
-- 架构说明：
--   1. 通知主体只存一条 notifications 行，投递范围写入 audience_json 供审计回溯。
--   2. notification_recipients 是「谁收到了这条通知」的事实表，created_at 从
--      notifications 冗余一份，使收件箱查询与未读计数无需 JOIN 排序。
--   3. notification_channels 是租户级渠道配置，替代已废弃的服务器级
--      automation_smtp_* / automation_*_webhook_url 配置。
--   4. 平台通知（kind='platform'）由超管发布时按租户各写一条，不做跨租户可见行，
--      因此所有 RLS 策略都能保持「行必须属于单一租户」的简单不变式。

CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    kind VARCHAR(24) NOT NULL CHECK (kind IN ('platform','tenant','group','personal')),
    title VARCHAR(256) NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    link VARCHAR(512) NOT NULL DEFAULT '',
    created_by BIGINT NOT NULL DEFAULT 0,
    audience_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_tenant_created ON notifications (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notification_recipients (
    notification_id UUID NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    tenant_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (notification_id, user_id)
);
-- 收件箱分页与未读计数共用 (tenant_id, user_id, created_at DESC) 索引。
CREATE INDEX IF NOT EXISTS idx_notification_recipients_inbox
    ON notification_recipients (tenant_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notification_channels (
    tenant_id BIGINT NOT NULL,
    channel VARCHAR(16) NOT NULL CHECK (channel IN ('in_app','email','feishu','slack')),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, channel)
);

-- 站内信是唯一默认启用渠道：为已存在租户播种默认行；读取接口另有缺失兜底，
-- 保证迁移后新建租户同样得到 enabled=TRUE 的 in_app 配置。
INSERT INTO notification_channels (tenant_id, channel, enabled, config_json, updated_by, updated_at)
SELECT id, 'in_app', TRUE, '{}'::jsonb, 0, NOW() FROM tenants
ON CONFLICT (tenant_id, channel) DO NOTHING;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'notifications', 'notification_recipients', 'notification_channels'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    END LOOP;
END $$;

-- 通知主体的可见性：系统身份（super_admin）用于投递写入；普通用户只能读到
-- 自己作为收件人的行；租户管理员可审计本租户的非个人通知。
DROP POLICY IF EXISTS notifications_scope ON notifications;
CREATE POLICY notifications_scope ON notifications FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR EXISTS (
        SELECT 1 FROM notification_recipients r
        WHERE r.notification_id = notifications.id
          AND r.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
          AND r.user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint
    )
    OR (
        current_setting('app.current_role', true) = 'tenant_admin'
        AND kind <> 'personal'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
    )
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        current_setting('app.current_role', true) = 'tenant_admin'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
    )
);

-- 收件人行只对本人可见：已读、未读计数与删除都在这张表上完成。
DROP POLICY IF EXISTS notification_recipients_scope ON notification_recipients;
CREATE POLICY notification_recipients_scope ON notification_recipients FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint
    )
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint
    )
);

-- 渠道配置是租户级资源：只有系统身份与当前租户管理员可以读写。
DROP POLICY IF EXISTS notification_channels_scope ON notification_channels;
CREATE POLICY notification_channels_scope ON notification_channels FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        current_setting('app.current_role', true) = 'tenant_admin'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
    )
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        current_setting('app.current_role', true) = 'tenant_admin'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
    )
);
