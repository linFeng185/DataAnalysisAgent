CREATE TABLE schema_sync_jobs (
    tenant_id bigint NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    datasource text NOT NULL,
    run_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('running','cancelling','succeeded','cancelled','failed')),
    phase text NOT NULL DEFAULT 'collecting',
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    table_count integer NOT NULL DEFAULT 0,
    error text NOT NULL DEFAULT '',
    snapshot_json jsonb,
    snapshot_version uuid,
    fingerprint text NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id,datasource)
);
ALTER TABLE schema_sync_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_sync_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY schema_sync_isolation ON schema_sync_jobs FOR ALL USING (
    current_setting('app.current_role',true)='super_admin'
    OR tenant_id=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
);
