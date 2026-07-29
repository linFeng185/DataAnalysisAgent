-- 长期记忆、向量补偿与会话归档生产表。

CREATE TABLE IF NOT EXISTS long_term_memories (
    id TEXT PRIMARY KEY,
    memory_type VARCHAR(32) NOT NULL,
    scope TEXT NOT NULL,
    visibility VARCHAR(16) NOT NULL DEFAULT 'private',
    tenant_id INT NOT NULL DEFAULT 1,
    owner_user_id INT NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    access_count INT NOT NULL DEFAULT 0,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    ttl_days INT,
    CONSTRAINT long_term_memories_visibility_check
        CHECK (visibility IN ('system', 'tenant', 'private')),
    CONSTRAINT long_term_memories_identity_check CHECK (
        (visibility = 'system' AND tenant_id = 0 AND owner_user_id = 0)
        OR (visibility = 'tenant' AND tenant_id > 0 AND owner_user_id = 0)
        OR (visibility = 'private' AND tenant_id > 0 AND owner_user_id > 0)
    ),
    CONSTRAINT long_term_memories_confidence_check
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT long_term_memories_ttl_check
        CHECK (ttl_days IS NULL OR ttl_days > 0)
);

CREATE INDEX IF NOT EXISTS ix_long_term_memories_visible
    ON long_term_memories (visibility, tenant_id, owner_user_id, memory_type);
CREATE INDEX IF NOT EXISTS ix_long_term_memories_expiry
    ON long_term_memories (created_at, ttl_days)
    WHERE ttl_days IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_long_term_memories_decay
    ON long_term_memories (memory_type, last_accessed_at, confidence);

CREATE TABLE IF NOT EXISTS pending_vector_sync (
    entry_id TEXT PRIMARY KEY REFERENCES long_term_memories(id) ON DELETE CASCADE,
    operation VARCHAR(16) NOT NULL,
    retry_count INT NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pending_vector_sync_operation_check
        CHECK (operation IN ('upsert', 'delete'))
);

CREATE TABLE IF NOT EXISTS sessions_archive (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    datasource TEXT NOT NULL DEFAULT '',
    first_query TEXT NOT NULL DEFAULT '',
    user_id INT NOT NULL,
    tenant_id INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_active_at TIMESTAMPTZ NOT NULL,
    turn_count INT NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_sessions_archive_identity
    ON sessions_archive (tenant_id, user_id, last_active_at DESC);

ALTER TABLE long_term_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE long_term_memories FORCE ROW LEVEL SECURITY;
ALTER TABLE sessions_archive ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions_archive FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS long_term_memories_read_scope ON long_term_memories;
CREATE POLICY long_term_memories_read_scope ON long_term_memories
    FOR SELECT USING (
        visibility = 'system'
        OR (
            visibility = 'tenant'
            AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        )
        OR (
            visibility = 'private'
            AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
            AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
    );

DROP POLICY IF EXISTS long_term_memories_write_scope ON long_term_memories;
CREATE POLICY long_term_memories_write_scope ON long_term_memories
    FOR ALL USING (
        (visibility = 'system' AND current_setting('app.current_role', true) = 'super_admin')
        OR (
            visibility = 'tenant'
            AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
            AND current_setting('app.current_role', true) IN ('super_admin', 'tenant_admin')
        )
        OR (
            visibility = 'private'
            AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
            AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
    ) WITH CHECK (
        (visibility = 'system' AND current_setting('app.current_role', true) = 'super_admin')
        OR (
            visibility = 'tenant'
            AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
            AND current_setting('app.current_role', true) IN ('super_admin', 'tenant_admin')
        )
        OR (
            visibility = 'private'
            AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
            AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
    );

DROP POLICY IF EXISTS long_term_memories_maintenance ON long_term_memories;
CREATE POLICY long_term_memories_maintenance ON long_term_memories
    FOR ALL USING (
        current_setting('app.current_role', true) = 'super_admin'
    ) WITH CHECK (
        current_setting('app.current_role', true) = 'super_admin'
    );

DROP POLICY IF EXISTS sessions_memory_maintenance ON sessions;
CREATE POLICY sessions_memory_maintenance ON sessions
    FOR ALL USING (
        current_setting('app.current_role', true) = 'super_admin'
    ) WITH CHECK (
        current_setting('app.current_role', true) = 'super_admin'
    );

DROP POLICY IF EXISTS sessions_archive_identity_isolation ON sessions_archive;
CREATE POLICY sessions_archive_identity_isolation ON sessions_archive
    FOR ALL USING (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    ) WITH CHECK (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    );

DROP POLICY IF EXISTS sessions_archive_maintenance ON sessions_archive;
CREATE POLICY sessions_archive_maintenance ON sessions_archive
    FOR ALL USING (
        current_setting('app.current_role', true) = 'super_admin'
    ) WITH CHECK (
        current_setting('app.current_role', true) = 'super_admin'
    );
