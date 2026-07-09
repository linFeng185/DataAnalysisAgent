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

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id INT DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1;
ALTER TABLE query_history ADD COLUMN IF NOT EXISTS user_id INT DEFAULT 0;
ALTER TABLE query_history ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1;

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
ALTER TABLE query_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sessions;
CREATE POLICY tenant_isolation ON sessions FOR ALL USING (
    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int);
DROP POLICY IF EXISTS user_isolation ON sessions;
CREATE POLICY user_isolation ON sessions FOR ALL USING (
    user_id = NULLIF(current_setting('app.current_user_id', true), '')::int);
