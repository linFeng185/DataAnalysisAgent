-- 报表工作台 Phase 1（PostgreSQL）。编号核对：017 为上一条迁移，本文件不与现有编号冲突。
-- SQLite 测试不执行本文件；ReportStore(dialect='sqlite') 使用应用层锁和参数化查询模拟认领。

CREATE TABLE IF NOT EXISTS report_jobs (
    id UUID PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    plan_json JSONB NOT NULL,
    plan_hash VARCHAR(80) NOT NULL,
    execution_mode VARCHAR(16) NOT NULL CHECK (execution_mode IN ('immediate', 'scheduled')),
    frequency VARCHAR(16),
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    max_runs INTEGER,
    runs_started INTEGER NOT NULL DEFAULT 0,
    schedule_end_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    page_ttl_days INTEGER NOT NULL DEFAULT 7 CHECK (page_ttl_days BETWEEN 1 AND 90),
    permanent BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(16) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','planning','queued','running','succeeded','partial','failed','cancelled','expired','deleted')),
    idempotency_key VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_report_jobs_due ON report_jobs (status, next_run_at) WHERE next_run_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_report_jobs_identity ON report_jobs (tenant_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS report_runs (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES report_jobs(id) ON DELETE CASCADE,
    tenant_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','partial','failed','cancelled')),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    run_key VARCHAR(180) NOT NULL UNIQUE,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    last_node VARCHAR(128),
    query_count INTEGER NOT NULL DEFAULT 0,
    row_count BIGINT NOT NULL DEFAULT 0,
    token_usage BIGINT NOT NULL DEFAULT 0,
    partial BOOLEAN NOT NULL DEFAULT FALSE,
    error_code VARCHAR(64),
    internal_error_message TEXT,
    user_error_message VARCHAR(500),
    trace_id VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_report_runs_identity ON report_runs (tenant_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS report_run_steps (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES report_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    node VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    input_hash VARCHAR(128),
    output_ref VARCHAR(500),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER,
    error_code VARCHAR(64),
    internal_error_message TEXT,
    user_error_message VARCHAR(500),
    UNIQUE (run_id, seq, attempt)
);

CREATE TABLE IF NOT EXISTS report_versions (
    id UUID PRIMARY KEY,
    report_id UUID NOT NULL,
    job_id UUID NOT NULL REFERENCES report_jobs(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES report_runs(id) ON DELETE CASCADE,
    tenant_id BIGINT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    document_json JSONB NOT NULL,
    data_manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    checksum VARCHAR(128) NOT NULL,
    expires_at TIMESTAMPTZ,
    is_latest BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (report_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_report_versions_latest ON report_versions (report_id) WHERE is_latest AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS report_shares (
    id UUID PRIMARY KEY,
    report_version_id UUID NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
    report_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(128) NOT NULL UNIQUE,
    code_hash VARCHAR(128),
    scope VARCHAR(16) NOT NULL CHECK (scope IN ('public','user')),
    user_scope VARCHAR(16) NOT NULL DEFAULT 'none' CHECK (user_scope IN ('none','specified','tenant')),
    user_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    allow_drilldown BOOLEAN NOT NULL DEFAULT FALSE,
    allow_download BOOLEAN NOT NULL DEFAULT FALSE,
    pin_latest BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    view_count BIGINT NOT NULL DEFAULT 0,
    last_viewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_report_shares_report ON report_shares (tenant_id, report_id, created_at DESC);

CREATE TABLE IF NOT EXISTS report_user_permissions (
    tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS report_quota_policies (
    tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period VARCHAR(32) NOT NULL DEFAULT 'calendar_month',
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    max_jobs_created INTEGER NOT NULL DEFAULT 20,
    max_runs_started INTEGER NOT NULL DEFAULT 50,
    max_concurrent_runs INTEGER NOT NULL DEFAULT 2,
    max_analysis_units_per_run INTEGER NOT NULL DEFAULT 12,
    max_queries_per_run INTEGER NOT NULL DEFAULT 16,
    max_rows_per_query BIGINT NOT NULL DEFAULT 100000,
    max_llm_tokens BIGINT NOT NULL DEFAULT 200000,
    max_page_bytes BIGINT NOT NULL DEFAULT 2097152,
    max_share_links INTEGER NOT NULL DEFAULT 20,
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS report_quota_usage (
    tenant_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    jobs_created INTEGER NOT NULL DEFAULT 0,
    runs_started INTEGER NOT NULL DEFAULT 0,
    concurrent_runs INTEGER NOT NULL DEFAULT 0,
    analysis_units INTEGER NOT NULL DEFAULT 0,
    queries BIGINT NOT NULL DEFAULT 0,
    rows BIGINT NOT NULL DEFAULT 0,
    llm_tokens BIGINT NOT NULL DEFAULT 0,
    share_links INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, user_id, period_start)
);

CREATE TABLE IF NOT EXISTS report_access_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT,
    user_id BIGINT,
    share_id UUID,
    report_id UUID,
    action VARCHAR(32) NOT NULL,
    result VARCHAR(32) NOT NULL,
    ip_hash VARCHAR(128),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_report_access_audit_lookup ON report_access_audit (tenant_id, report_id, created_at DESC);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'report_jobs', 'report_runs', 'report_run_steps', 'report_versions',
        'report_shares', 'report_user_permissions', 'report_quota_policies',
        'report_quota_usage', 'report_access_audit'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    END LOOP;
END $$;

DROP POLICY IF EXISTS report_jobs_identity ON report_jobs;
CREATE POLICY report_jobs_identity ON report_jobs FOR ALL USING (
    current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
);

DROP POLICY IF EXISTS report_runs_identity ON report_runs;
CREATE POLICY report_runs_identity ON report_runs FOR ALL USING (
    current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
);

DROP POLICY IF EXISTS report_run_steps_identity ON report_run_steps;
CREATE POLICY report_run_steps_identity ON report_run_steps FOR ALL USING (
    EXISTS (
        SELECT 1 FROM report_runs r
        WHERE r.id = report_run_steps.run_id
          AND (
              current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
              OR (r.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
                  AND r.user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
          )
    )
);

DROP POLICY IF EXISTS report_versions_identity ON report_versions;
CREATE POLICY report_versions_identity ON report_versions FOR ALL USING (
    current_setting('app.report_share_access', true) = 'true'
    OR current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
);

DROP POLICY IF EXISTS report_shares_identity ON report_shares;
CREATE POLICY report_shares_identity ON report_shares FOR ALL USING (
    current_setting('app.report_share_access', true) = 'true'
    OR current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND created_by = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
);

DROP POLICY IF EXISTS report_user_permissions_identity ON report_user_permissions;
CREATE POLICY report_user_permissions_identity ON report_user_permissions FOR ALL USING (
    current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
);

DROP POLICY IF EXISTS report_quota_policies_identity ON report_quota_policies;
CREATE POLICY report_quota_policies_identity ON report_quota_policies FOR ALL USING (
    current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
);

DROP POLICY IF EXISTS report_quota_usage_identity ON report_quota_usage;
CREATE POLICY report_quota_usage_identity ON report_quota_usage FOR ALL USING (
    current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
);

DROP POLICY IF EXISTS report_access_audit_identity ON report_access_audit;
CREATE POLICY report_access_audit_identity ON report_access_audit FOR ALL USING (
    current_setting('app.current_role', true) IN ('super_admin','tenant_admin')
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
);
