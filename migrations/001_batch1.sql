-- 第 1 批迁移：用户/租户/权限/审计（幂等）
CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW());
INSERT INTO tenants (id, name) VALUES (1, 'default') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY, username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'analyst',
    tenant_id INT NOT NULL REFERENCES tenants(id) DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE(username, tenant_id));

CREATE TABLE IF NOT EXISTS datasource_permissions (
    id SERIAL PRIMARY KEY, datasource_name VARCHAR(64) NOT NULL,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    owner_user_id INT NOT NULL REFERENCES users(id),
    visibility VARCHAR(16) DEFAULT 'private',
    access_level VARCHAR(16) DEFAULT 'read',
    allowed_columns TEXT[] DEFAULT '{}',
    row_filter_sql TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(datasource_name, tenant_id));

CREATE TABLE IF NOT EXISTS query_audit_log (
    id SERIAL PRIMARY KEY, user_id INT NOT NULL, tenant_id INT NOT NULL,
    datasource VARCHAR(64), sql_hash VARCHAR(64),
    row_count INT DEFAULT 0, duration_ms INT DEFAULT 0,
    success BOOLEAN DEFAULT TRUE, error_message TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON query_audit_log(tenant_id, created_at DESC);

-- 会话、历史和知识文件必须先建表，再执行兼容旧库的 ALTER。
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT DEFAULT '', datasource TEXT DEFAULT '', first_query TEXT DEFAULT '',
    user_id INT NOT NULL DEFAULT 0, tenant_id INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(), last_active_at TIMESTAMPTZ DEFAULT NOW(),
    turn_count INT DEFAULT 0);

CREATE TABLE IF NOT EXISTS query_history (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL, sql TEXT DEFAULT '', datasource TEXT DEFAULT '', session_id TEXT DEFAULT '',
    user_id INT NOT NULL DEFAULT 0, tenant_id INT NOT NULL DEFAULT 1,
    success BOOLEAN DEFAULT TRUE, row_count INT DEFAULT 0,
    final_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW());

CREATE TABLE IF NOT EXISTS knowledge_files (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL, content_type TEXT DEFAULT 'application/octet-stream',
    file_data BYTEA NOT NULL, size BIGINT DEFAULT 0,
    user_id INT NOT NULL DEFAULT 0, tenant_id INT NOT NULL DEFAULT 1,
    uploaded_at TIMESTAMPTZ DEFAULT NOW());

CREATE INDEX IF NOT EXISTS idx_sessions_identity_active
    ON sessions (tenant_id, user_id, last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_query_history_identity_created
    ON query_history (tenant_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_files_identity_uploaded
    ON knowledge_files (tenant_id, user_id, uploaded_at DESC);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id INT DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1;
ALTER TABLE query_history ADD COLUMN IF NOT EXISTS user_id INT DEFAULT 0;
ALTER TABLE query_history ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1;
ALTER TABLE query_history ADD COLUMN IF NOT EXISTS final_result JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS user_id INT DEFAULT 0;
ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1;

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS llm_api_key TEXT DEFAULT '';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS llm_base_url TEXT DEFAULT '';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS llm_model TEXT DEFAULT '';

CREATE TABLE IF NOT EXISTS mcp_servers (
    id SERIAL PRIMARY KEY, name VARCHAR(64) NOT NULL,
    tenant_id INT NOT NULL DEFAULT 1 REFERENCES tenants(id),
    transport VARCHAR(32) NOT NULL DEFAULT 'stdio',
    command TEXT DEFAULT '', args TEXT DEFAULT '',
    url TEXT DEFAULT '', env_vars JSONB DEFAULT '{}',
    description TEXT DEFAULT '', is_builtin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(name, tenant_id));

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE query_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_history FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_files FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sessions;
DROP POLICY IF EXISTS user_isolation ON sessions;
DROP POLICY IF EXISTS session_identity_isolation ON sessions;
CREATE POLICY session_identity_isolation ON sessions FOR ALL USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
);
DROP POLICY IF EXISTS history_identity_isolation ON query_history;
CREATE POLICY history_identity_isolation ON query_history FOR ALL USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
);
DROP POLICY IF EXISTS knowledge_file_identity_isolation ON knowledge_files;
CREATE POLICY knowledge_file_identity_isolation ON knowledge_files FOR ALL USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
) WITH CHECK (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
);
