-- 平台管理基础：固定超级管理员、登录防爆破和页面数据源持久化。

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_reserved_super_admin;
ALTER TABLE users ADD CONSTRAINT ck_users_reserved_super_admin CHECK (
    (id = 1 AND role = 'super_admin')
    OR (id <> 1 AND role <> 'super_admin')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_global ON users (LOWER(username));

DROP POLICY IF EXISTS mcp_servers_read_scope ON mcp_servers;
CREATE POLICY mcp_servers_read_scope ON mcp_servers FOR SELECT USING (
    (current_setting('app.current_role', true) = 'super_admin'
        AND NULLIF(current_setting('app.current_user_id', true), '')::int = 1)
    OR (
        scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    )
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

DROP POLICY IF EXISTS knowledge_files_read_scope ON knowledge_files;
CREATE POLICY knowledge_files_read_scope ON knowledge_files FOR SELECT USING (
    (current_setting('app.current_role', true) = 'super_admin'
        AND NULLIF(current_setting('app.current_user_id', true), '')::int = 1)
    OR (
        knowledge_scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    )
    OR (
        knowledge_scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

CREATE TABLE IF NOT EXISTS datasource_configs (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    owner_user_id INT NOT NULL REFERENCES users(id),
    dialect VARCHAR(32) NOT NULL,
    version VARCHAR(32) NOT NULL DEFAULT '',
    host VARCHAR(255) NOT NULL DEFAULT 'localhost',
    port INT NOT NULL DEFAULT 0,
    database_name VARCHAR(255) NOT NULL DEFAULT '',
    username VARCHAR(255) NOT NULL DEFAULT '',
    encrypted_password TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    extra_params JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name)
);

SELECT setval(
    pg_get_serial_sequence('tenants', 'id'),
    GREATEST(COALESCE((SELECT MAX(id) FROM tenants), 1), 1),
    TRUE
);
SELECT setval(
    pg_get_serial_sequence('users', 'id'),
    GREATEST(COALESCE((SELECT MAX(id) FROM users), 1), 1),
    TRUE
);
