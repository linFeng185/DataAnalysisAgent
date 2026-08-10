-- 记录通过只读校验和执行验证的 SQL 观察，用于租户/用户隔离的知识自进化。

CREATE TABLE IF NOT EXISTS knowledge_sql_observations (
    tenant_id BIGINT NOT NULL,
    owner_user_id BIGINT NOT NULL,
    fingerprint TEXT NOT NULL,
    dialect TEXT NOT NULL,
    datasource TEXT NOT NULL DEFAULT '',
    user_query TEXT NOT NULL,
    normalized_sql TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1 CHECK (observation_count > 0),
    last_execution_ms DOUBLE PRECISION,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, owner_user_id, fingerprint)
);

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
