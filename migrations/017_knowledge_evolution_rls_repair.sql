-- 修复早期 015 已建表但未启用 RLS 的数据库，保持租户和用户隔离策略幂等。

CREATE INDEX IF NOT EXISTS idx_knowledge_sql_observations_last_seen
    ON knowledge_sql_observations (tenant_id, owner_user_id, last_seen_at DESC);

ALTER TABLE knowledge_sql_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_sql_observations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS knowledge_sql_observations_identity ON knowledge_sql_observations;
CREATE POLICY knowledge_sql_observations_identity ON knowledge_sql_observations
    FOR ALL USING (
        current_setting('app.current_role', true) = 'super_admin'
        OR (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
            AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint
        )
    ) WITH CHECK (
        current_setting('app.current_role', true) = 'super_admin'
        OR (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
            AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint
        )
    );
