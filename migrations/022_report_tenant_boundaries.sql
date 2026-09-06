-- 修正旧迁移中租户管理员未绑定 tenant_id 的 USING 分支，保留系统调度所需平台身份。
DROP POLICY IF EXISTS report_jobs_identity ON report_jobs;
CREATE POLICY report_jobs_identity ON report_jobs FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);

DROP POLICY IF EXISTS report_runs_identity ON report_runs;
CREATE POLICY report_runs_identity ON report_runs FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);

DROP POLICY IF EXISTS report_run_steps_identity ON report_run_steps;
CREATE POLICY report_run_steps_identity ON report_run_steps FOR ALL USING (
    EXISTS (SELECT 1 FROM report_runs parent_run WHERE parent_run.id=report_run_steps.run_id)
);

DROP POLICY IF EXISTS report_versions_identity ON report_versions;
CREATE POLICY report_versions_identity ON report_versions FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND EXISTS (SELECT 1 FROM report_jobs parent_job WHERE parent_job.id=report_versions.job_id))
);
CREATE POLICY report_versions_share_read ON report_versions FOR SELECT USING (
    current_setting('app.report_share_access', true) = 'true'
);

DROP POLICY IF EXISTS report_shares_identity ON report_shares;
CREATE POLICY report_shares_identity ON report_shares FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR created_by = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);
CREATE POLICY report_shares_token_read ON report_shares FOR SELECT USING (
    current_setting('app.report_share_access', true) = 'true'
);

DROP POLICY IF EXISTS report_user_permissions_identity ON report_user_permissions;
CREATE POLICY report_user_permissions_identity ON report_user_permissions FOR SELECT USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);
CREATE POLICY report_user_permissions_manage ON report_user_permissions FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND current_setting('app.current_role', true) = 'tenant_admin')
);

DROP POLICY IF EXISTS report_quota_policies_identity ON report_quota_policies;
CREATE POLICY report_quota_policies_identity ON report_quota_policies FOR SELECT USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);
CREATE POLICY report_quota_policies_manage ON report_quota_policies FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND current_setting('app.current_role', true) = 'tenant_admin')
);

DROP POLICY IF EXISTS report_quota_usage_identity ON report_quota_usage;
CREATE POLICY report_quota_usage_identity ON report_quota_usage FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);

DROP POLICY IF EXISTS report_access_audit_identity ON report_access_audit;
CREATE POLICY report_access_audit_identity ON report_access_audit FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);
