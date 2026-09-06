CREATE TABLE IF NOT EXISTS query_quota_policies (
    tenant_id bigint PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_per_hour bigint NOT NULL CHECK (tenant_per_hour >= 0),
    user_per_hour bigint NOT NULL CHECK (user_per_hour >= 0 AND user_per_hour <= tenant_per_hour),
    updated_by bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS query_rate_usage (
    tenant_id bigint NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id bigint NOT NULL,
    window_hour bigint NOT NULL,
    used bigint NOT NULL CHECK (used >= 0),
    PRIMARY KEY (tenant_id,user_id,window_hour)
);
ALTER TABLE query_quota_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_quota_policies FORCE ROW LEVEL SECURITY;
ALTER TABLE query_rate_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_rate_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY query_quota_read ON query_quota_policies FOR SELECT USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
    AND current_setting('app.current_role',true) <> 'super_admin'
);
CREATE POLICY query_quota_manage ON query_quota_policies FOR ALL USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
    AND current_setting('app.current_role',true) = 'tenant_admin'
);
CREATE POLICY query_usage_identity ON query_rate_usage FOR ALL USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
    AND current_setting('app.current_role',true) <> 'super_admin'
);
