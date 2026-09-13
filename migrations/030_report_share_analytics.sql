-- 链接管理与访问统计（需求 3 / C 批，PostgreSQL）。编号核对：029 为上一条迁移，030 未被占用。
-- SQLite 测试不执行本文件；ReportStore(memory=True) 用等价的 Python 分支实现同样语义。

-- 一次分享访问的明细：登录访客记录 user_id，匿名访客按 0 记录。
CREATE TABLE IF NOT EXISTS report_share_visits (
    id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    share_id UUID NOT NULL REFERENCES report_shares(id) ON DELETE CASCADE,
    report_id UUID NOT NULL,
    visitor_kind TEXT NOT NULL CHECK (visitor_kind IN ('user', 'anonymous')),
    user_id BIGINT NOT NULL DEFAULT 0,
    ip_hash VARCHAR(128) NOT NULL DEFAULT '',
    unlocked BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK (duration_seconds >= 0)
);

CREATE INDEX IF NOT EXISTS idx_report_share_visits_share ON report_share_visits (tenant_id, share_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_share_visits_user ON report_share_visits (tenant_id, user_id, started_at DESC);

-- 租户级报表设置。record_share_visitor_identity 为需求 3.2 的身份记录开关，
-- max_permanent_reports_per_user 为需求 2.4 的永久报表数量上限（0 表示不限制）。
CREATE TABLE IF NOT EXISTS report_tenant_settings (
    tenant_id BIGINT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    record_share_visitor_identity BOOLEAN NOT NULL DEFAULT TRUE,
    max_permanent_reports_per_user INTEGER NOT NULL DEFAULT 0 CHECK (max_permanent_reports_per_user >= 0),
    updated_by BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE report_share_visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_share_visits FORCE ROW LEVEL SECURITY;
ALTER TABLE report_tenant_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_tenant_settings FORCE ROW LEVEL SECURITY;

-- 管理侧访问：租户管理员可看全租户，其他角色只能看自己创建的分享所对应的访问。
-- 访问行的所有者是分享创建者而不是访客本身，因此这里必须经 report_shares 判定归属。
DROP POLICY IF EXISTS report_share_visits_identity ON report_share_visits;
CREATE POLICY report_share_visits_identity ON report_share_visits FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR EXISTS (
                 SELECT 1 FROM report_shares s
                 WHERE s.id = report_share_visits.share_id
                   AND s.created_by = NULLIF(current_setting('app.current_user_id', true), '')::bigint
             )))
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR EXISTS (
                 SELECT 1 FROM report_shares s
                 WHERE s.id = report_share_visits.share_id
                   AND s.created_by = NULLIF(current_setting('app.current_user_id', true), '')::bigint
             )))
);

-- 公开侧写入：分享页埋点不经登录身份，靠 report_share_access 标记放行 INSERT 与 UPDATE。
-- 不给公开侧 SELECT/DELETE 权限：读取只走管理侧身份，删除只随分享级联。
DROP POLICY IF EXISTS report_share_visits_public_insert ON report_share_visits;
CREATE POLICY report_share_visits_public_insert ON report_share_visits FOR INSERT WITH CHECK (
    current_setting('app.report_share_access', true) = 'true'
);
DROP POLICY IF EXISTS report_share_visits_public_update ON report_share_visits;
CREATE POLICY report_share_visits_public_update ON report_share_visits FOR UPDATE USING (
    current_setting('app.report_share_access', true) = 'true'
) WITH CHECK (
    current_setting('app.report_share_access', true) = 'true'
);

-- 租户设置：读取按租户隔离（公开分享上报需要按分享租户读取身份记录开关），
-- 管理端「仅租户管理员」的角色校验在 API 层完成，写入侧在这里再收一次。
DROP POLICY IF EXISTS report_tenant_settings_identity ON report_tenant_settings;
CREATE POLICY report_tenant_settings_identity ON report_tenant_settings FOR SELECT USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
);
DROP POLICY IF EXISTS report_tenant_settings_manage ON report_tenant_settings;
CREATE POLICY report_tenant_settings_manage ON report_tenant_settings FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND current_setting('app.current_role', true) = 'tenant_admin')
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND current_setting('app.current_role', true) = 'tenant_admin')
);
