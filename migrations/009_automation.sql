CREATE TABLE IF NOT EXISTS analysis_schedules (
    id UUID PRIMARY KEY,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    user_id INT NOT NULL REFERENCES users(id),
    name VARCHAR(128) NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('insight', 'report')),
    datasource VARCHAR(64) NOT NULL,
    sql_text TEXT NOT NULL,
    dialect VARCHAR(32) NOT NULL,
    frequency VARCHAR(16) NOT NULL CHECK (frequency IN ('hourly', 'daily', 'weekly', 'monthly')),
    threshold_pct NUMERIC(8,2) NOT NULL DEFAULT 10 CHECK (threshold_pct >= 0 AND threshold_pct <= 10000),
    channels JSONB NOT NULL DEFAULT '["in_app"]',
    recipient_email VARCHAR(320) NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMPTZ NOT NULL,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analysis_schedules_due
    ON analysis_schedules (next_run_at) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_analysis_schedules_owner
    ON analysis_schedules (tenant_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS analysis_schedule_runs (
    id UUID PRIMARY KEY,
    schedule_id UUID NOT NULL REFERENCES analysis_schedules(id) ON DELETE CASCADE,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    user_id INT NOT NULL REFERENCES users(id),
    status VARCHAR(16) NOT NULL CHECK (status IN ('success', 'failed')),
    result_payload JSONB NOT NULL DEFAULT '{}',
    error_message VARCHAR(500) NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_analysis_schedule_runs_latest
    ON analysis_schedule_runs (schedule_id, started_at DESC);

CREATE TABLE IF NOT EXISTS analysis_notifications (
    id UUID PRIMARY KEY,
    schedule_id UUID NOT NULL REFERENCES analysis_schedules(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES analysis_schedule_runs(id) ON DELETE CASCADE,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    user_id INT NOT NULL REFERENCES users(id),
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('insight', 'report')),
    title VARCHAR(256) NOT NULL,
    body TEXT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analysis_notifications_user
    ON analysis_notifications (tenant_id, user_id, created_at DESC);

ALTER TABLE analysis_schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis_schedule_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis_notifications ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS analysis_schedules_scope ON analysis_schedules;
CREATE POLICY analysis_schedules_scope ON analysis_schedules
USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
            OR current_setting('app.current_role', true) = 'tenant_admin'
        )
    )
);

DROP POLICY IF EXISTS analysis_schedule_runs_scope ON analysis_schedule_runs;
CREATE POLICY analysis_schedule_runs_scope ON analysis_schedule_runs
USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
            OR current_setting('app.current_role', true) = 'tenant_admin'
        )
    )
);

DROP POLICY IF EXISTS analysis_notifications_scope ON analysis_notifications;
CREATE POLICY analysis_notifications_scope ON analysis_notifications
USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);
