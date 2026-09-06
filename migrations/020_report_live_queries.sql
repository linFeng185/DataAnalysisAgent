-- 实时钻取模板仅保存在私有表，公开页面仅携带随机标识。
CREATE TABLE IF NOT EXISTS report_live_queries (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES report_runs(id) ON DELETE CASCADE,
    tenant_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    datasource VARCHAR(128) NOT NULL,
    dialect VARCHAR(32) NOT NULL,
    sql_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_report_live_queries_run ON report_live_queries (tenant_id, run_id);
ALTER TABLE report_live_queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_live_queries FORCE ROW LEVEL SECURITY;
CREATE POLICY report_live_queries_identity ON report_live_queries FOR ALL USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::BIGINT
    AND (user_id = NULLIF(current_setting('app.current_user_id', true), '')::BIGINT
         OR current_setting('app.current_role', true) = 'tenant_admin')
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::BIGINT
    AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::BIGINT
);
