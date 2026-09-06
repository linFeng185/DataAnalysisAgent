CREATE TABLE IF NOT EXISTS business_metrics (
    id TEXT PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    code VARCHAR(64) NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    status VARCHAR(16) NOT NULL CHECK (status IN ('draft','pending','approved','rejected')),
    definition JSONB NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    reviewed_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL,
    reviewed_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    review_note TEXT NOT NULL DEFAULT '',
    UNIQUE (tenant_id, code, version)
);
CREATE INDEX IF NOT EXISTS business_metrics_effective ON business_metrics(tenant_id, code, status, effective_at);
ALTER TABLE business_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_metrics FORCE ROW LEVEL SECURITY;
CREATE POLICY business_metrics_read ON business_metrics FOR SELECT USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::INTEGER
    AND (
        current_setting('app.current_role', true) = 'tenant_admin'
        OR (
            current_setting('app.current_role', true) IN ('analyst','viewer')
            AND status = 'approved' AND effective_at <= NOW()
        )
    )
);
CREATE POLICY business_metrics_insert ON business_metrics FOR INSERT WITH CHECK (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::INTEGER
    AND current_setting('app.current_role', true) = 'tenant_admin'
    AND created_by = NULLIF(current_setting('app.current_user_id', true), '')::INTEGER
);
CREATE POLICY business_metrics_update ON business_metrics FOR UPDATE USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::INTEGER
    AND current_setting('app.current_role', true) = 'tenant_admin'
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::INTEGER
    AND current_setting('app.current_role', true) = 'tenant_admin'
);
