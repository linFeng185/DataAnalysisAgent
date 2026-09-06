-- 无请求的后台身份才具有基础设施权限；用户 super_admin 不自动成为后台身份。
CREATE OR REPLACE FUNCTION app_is_background() RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT current_setting('app.current_role',true) = 'super_admin'
       AND current_setting('app.current_tenant_id',true) = '0'
       AND current_setting('app.current_user_id',true) = '0'
$$;

-- 范围管理只允许平台 system、同租户 tenant_admin、本人 private。
CREATE OR REPLACE FUNCTION app_scope_manage(resource_scope text, resource_tenant bigint, resource_owner bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT app_is_background() OR
      (resource_scope IN ('system','global') AND current_setting('app.current_role',true)='super_admin') OR
      (current_setting('app.current_role',true)<>'super_admin'
       AND resource_tenant=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
       AND ((resource_scope='tenant' AND current_setting('app.current_role',true)='tenant_admin') OR
            (resource_scope='private' AND resource_owner=NULLIF(current_setting('app.current_user_id',true),'')::bigint)))
$$;
CREATE OR REPLACE FUNCTION app_scope_read(resource_scope text, resource_tenant bigint, resource_owner bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT app_is_background() OR resource_scope IN ('system','global') OR
      (current_setting('app.current_role',true)<>'super_admin'
       AND resource_tenant=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
       AND (resource_scope='tenant' OR (resource_scope='private' AND resource_owner=NULLIF(current_setting('app.current_user_id',true),'')::bigint)))
$$;
CREATE OR REPLACE FUNCTION app_report_identity(resource_tenant bigint, resource_owner bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT app_is_background() OR
      (current_setting('app.current_role',true)<>'super_admin'
       AND resource_tenant=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
       AND (current_setting('app.current_role',true)='tenant_admin'
            OR resource_owner=NULLIF(current_setting('app.current_user_id',true),'')::bigint))
$$;

CREATE TABLE knowledge_emergency_grants (
    id uuid PRIMARY KEY,
    tenant_id bigint NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    file_id bigint NOT NULL REFERENCES knowledge_files(id) ON DELETE CASCADE,
    scope text NOT NULL CHECK (scope IN ('tenant','private')),
    owner_user_id bigint NOT NULL,
    issued_by bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    grantee_user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (issued_by<>grantee_user_id),
    CHECK (expires_at>created_at AND expires_at<=created_at+interval '60 minutes')
);
CREATE TABLE governance_audit (
    id bigserial PRIMARY KEY,
    tenant_id bigint NOT NULL,
    actor_user_id bigint NOT NULL,
    action text NOT NULL,
    resource text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE mcp_security_blocks (
    tenant_id bigint NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    scope text NOT NULL CHECK (scope IN ('tenant','private')),
    owner_user_id bigint NOT NULL,
    name text NOT NULL,
    blocked boolean NOT NULL,
    reason text NOT NULL,
    updated_by bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,scope,owner_user_id,name)
);
ALTER TABLE knowledge_emergency_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_emergency_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE governance_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE governance_audit FORCE ROW LEVEL SECURITY;
ALTER TABLE mcp_security_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE mcp_security_blocks FORCE ROW LEVEL SECURITY;
CREATE POLICY emergency_read ON knowledge_emergency_grants FOR SELECT USING (
    app_is_background() OR
    (issued_by=NULLIF(current_setting('app.current_user_id',true),'')::bigint AND tenant_id=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint) OR
    (grantee_user_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint AND current_setting('app.current_role',true)='super_admin')
);
CREATE POLICY emergency_insert ON knowledge_emergency_grants FOR INSERT WITH CHECK (
    current_setting('app.current_role',true)<>'super_admin'
    AND issued_by=NULLIF(current_setting('app.current_user_id',true),'')::bigint
    AND tenant_id=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
    AND app_scope_manage(scope,tenant_id,owner_user_id)
);
CREATE POLICY emergency_revoke ON knowledge_emergency_grants FOR UPDATE USING (
    current_setting('app.current_role',true)<>'super_admin'
    AND issued_by=NULLIF(current_setting('app.current_user_id',true),'')::bigint
    AND tenant_id=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint
);
-- 受让人行锁需要 UPDATE 权限；应用只执行 FOR UPDATE，不提供编辑入口。
CREATE POLICY emergency_read_lock ON knowledge_emergency_grants FOR UPDATE USING (
    current_setting('app.current_role',true)='super_admin'
    AND grantee_user_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint
) WITH CHECK (false);
CREATE POLICY governance_append ON governance_audit FOR INSERT WITH CHECK (
    actor_user_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint
    AND (current_setting('app.current_role',true)='super_admin' OR tenant_id=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint)
);
CREATE POLICY governance_read ON governance_audit FOR SELECT USING (
    actor_user_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint OR
    (current_setting('app.current_role',true)='tenant_admin' AND tenant_id=NULLIF(current_setting('app.current_tenant_id',true),'')::bigint)
);
CREATE POLICY mcp_block_read ON mcp_security_blocks FOR SELECT USING (
    current_setting('app.current_role',true)='super_admin' OR
    app_scope_read(scope,tenant_id,owner_user_id)
);
CREATE POLICY mcp_block_manage ON mcp_security_blocks FOR ALL USING (current_setting('app.current_role',true)='super_admin');

DROP POLICY IF EXISTS knowledge_files_read_scope ON knowledge_files;
DROP POLICY IF EXISTS knowledge_files_insert_scope ON knowledge_files;
DROP POLICY IF EXISTS knowledge_files_update_scope ON knowledge_files;
DROP POLICY IF EXISTS knowledge_files_delete_scope ON knowledge_files;
CREATE POLICY knowledge_files_read_scope ON knowledge_files FOR SELECT USING (app_scope_read(knowledge_scope,tenant_id,user_id));
CREATE POLICY knowledge_files_insert_scope ON knowledge_files FOR INSERT WITH CHECK (app_scope_manage(knowledge_scope,tenant_id,user_id));
CREATE POLICY knowledge_files_update_scope ON knowledge_files FOR UPDATE USING (app_scope_manage(knowledge_scope,tenant_id,user_id));
CREATE POLICY knowledge_files_delete_scope ON knowledge_files FOR DELETE USING (app_scope_manage(knowledge_scope,tenant_id,user_id));
CREATE POLICY knowledge_files_emergency_read ON knowledge_files FOR SELECT USING (
    current_setting('app.current_role',true)='super_admin' AND EXISTS (
        SELECT 1 FROM knowledge_emergency_grants grant_row
        WHERE grant_row.id=NULLIF(current_setting('app.knowledge_emergency_grant',true),'')::uuid
          AND grant_row.file_id=knowledge_files.id AND grant_row.tenant_id=knowledge_files.tenant_id
          AND grant_row.scope=knowledge_files.knowledge_scope AND grant_row.owner_user_id=knowledge_files.user_id
          AND grant_row.grantee_user_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint
          AND grant_row.revoked_at IS NULL AND grant_row.expires_at>clock_timestamp()
    )
);
DROP POLICY IF EXISTS knowledge_tags_read_scope ON knowledge_tags;
DROP POLICY IF EXISTS knowledge_tags_insert_scope ON knowledge_tags;
DROP POLICY IF EXISTS knowledge_tags_update_scope ON knowledge_tags;
CREATE POLICY knowledge_tags_read_scope ON knowledge_tags FOR SELECT USING (app_scope_read(scope,tenant_id,owner_user_id));
CREATE POLICY knowledge_tags_insert_scope ON knowledge_tags FOR INSERT WITH CHECK (app_scope_manage(scope,tenant_id,owner_user_id));
CREATE POLICY knowledge_tags_update_scope ON knowledge_tags FOR UPDATE USING (app_scope_manage(scope,tenant_id,owner_user_id));

DROP POLICY IF EXISTS mcp_servers_read_scope ON mcp_servers;
DROP POLICY IF EXISTS mcp_servers_write_scope ON mcp_servers;
DROP POLICY IF EXISTS mcp_servers_update_scope ON mcp_servers;
DROP POLICY IF EXISTS mcp_servers_delete_scope ON mcp_servers;
CREATE POLICY mcp_servers_read_scope ON mcp_servers FOR SELECT USING (app_scope_read(scope,tenant_id,owner_user_id));
CREATE POLICY mcp_servers_write_scope ON mcp_servers FOR INSERT WITH CHECK (app_scope_manage(scope,tenant_id,owner_user_id));
CREATE POLICY mcp_servers_update_scope ON mcp_servers FOR UPDATE USING (app_scope_manage(scope,tenant_id,owner_user_id));
CREATE POLICY mcp_servers_delete_scope ON mcp_servers FOR DELETE USING (app_scope_manage(scope,tenant_id,owner_user_id));

DROP POLICY IF EXISTS report_jobs_identity ON report_jobs;
CREATE POLICY report_jobs_identity ON report_jobs FOR ALL USING (app_report_identity(tenant_id,user_id));
DROP POLICY IF EXISTS report_runs_identity ON report_runs;
CREATE POLICY report_runs_identity ON report_runs FOR ALL USING (app_report_identity(tenant_id,user_id));
DROP POLICY IF EXISTS report_versions_identity ON report_versions;
CREATE POLICY report_versions_identity ON report_versions FOR ALL USING (
    app_is_background() OR (current_setting('app.current_role',true)<>'super_admin' AND EXISTS (
        SELECT 1 FROM report_jobs job WHERE job.id=report_versions.job_id AND job.tenant_id=report_versions.tenant_id
    ))
);
DROP POLICY IF EXISTS report_shares_identity ON report_shares;
CREATE POLICY report_shares_identity ON report_shares FOR ALL USING (app_report_identity(tenant_id,created_by));
DROP POLICY IF EXISTS report_user_permissions_identity ON report_user_permissions;
DROP POLICY IF EXISTS report_user_permissions_manage ON report_user_permissions;
CREATE POLICY report_user_permissions_identity ON report_user_permissions FOR SELECT USING (app_report_identity(tenant_id,user_id));
CREATE POLICY report_user_permissions_manage ON report_user_permissions FOR ALL USING (app_scope_manage('tenant',tenant_id,0));
DROP POLICY IF EXISTS report_quota_policies_identity ON report_quota_policies;
DROP POLICY IF EXISTS report_quota_policies_manage ON report_quota_policies;
CREATE POLICY report_quota_policies_identity ON report_quota_policies FOR SELECT USING (app_report_identity(tenant_id,user_id));
CREATE POLICY report_quota_policies_manage ON report_quota_policies FOR ALL USING (app_scope_manage('tenant',tenant_id,0));
DROP POLICY IF EXISTS report_quota_usage_identity ON report_quota_usage;
CREATE POLICY report_quota_usage_identity ON report_quota_usage FOR ALL USING (app_report_identity(tenant_id,user_id));
DROP POLICY IF EXISTS report_access_audit_identity ON report_access_audit;
CREATE POLICY report_access_audit_identity ON report_access_audit FOR ALL USING (app_report_identity(tenant_id,user_id));
DROP POLICY IF EXISTS report_live_queries_identity ON report_live_queries;
CREATE POLICY report_live_queries_identity ON report_live_queries FOR ALL USING (app_report_identity(tenant_id,user_id));
