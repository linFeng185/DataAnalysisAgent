-- Skill 使用受管目录保存；本迁移为 MCP 增加 system/tenant/private 三级作用域。

CREATE TABLE IF NOT EXISTS mcp_servers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    scope VARCHAR(16) NOT NULL DEFAULT 'tenant',
    tenant_id INT,
    owner_user_id INT NOT NULL DEFAULT 0,
    transport VARCHAR(32) NOT NULL DEFAULT 'stdio',
    command TEXT DEFAULT '',
    args TEXT DEFAULT '',
    url TEXT DEFAULT '',
    env_vars JSONB DEFAULT '{}',
    description TEXT DEFAULT '',
    is_builtin BOOLEAN DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'tenant';
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS owner_user_id INT NOT NULL DEFAULT 0;
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE mcp_servers ALTER COLUMN tenant_id DROP NOT NULL;

UPDATE mcp_servers
SET scope = CASE WHEN is_builtin THEN 'system' ELSE 'tenant' END
WHERE scope IS NULL OR scope NOT IN ('system', 'tenant', 'private');

UPDATE mcp_servers SET tenant_id = NULL, owner_user_id = 0 WHERE scope = 'system';
UPDATE mcp_servers SET owner_user_id = 0 WHERE scope = 'tenant';

ALTER TABLE mcp_servers DROP CONSTRAINT IF EXISTS mcp_servers_name_tenant_id_key;
DROP INDEX IF EXISTS uq_mcp_servers_scope_owner_name;
CREATE UNIQUE INDEX uq_mcp_servers_scope_owner_name ON mcp_servers (
    scope,
    COALESCE(tenant_id, 0),
    owner_user_id,
    name
);

ALTER TABLE mcp_servers DROP CONSTRAINT IF EXISTS ck_mcp_servers_scope;
ALTER TABLE mcp_servers ADD CONSTRAINT ck_mcp_servers_scope CHECK (
    scope IN ('system', 'tenant', 'private')
    AND (scope <> 'system' OR (tenant_id IS NULL AND owner_user_id = 0))
    AND (scope <> 'tenant' OR (tenant_id IS NOT NULL AND owner_user_id = 0))
    AND (scope <> 'private' OR (tenant_id IS NOT NULL AND owner_user_id >= 0))
);

ALTER TABLE mcp_servers ENABLE ROW LEVEL SECURITY;
ALTER TABLE mcp_servers FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mcp_servers_read_scope ON mcp_servers;
DROP POLICY IF EXISTS mcp_servers_write_scope ON mcp_servers;
DROP POLICY IF EXISTS mcp_servers_update_scope ON mcp_servers;
DROP POLICY IF EXISTS mcp_servers_delete_scope ON mcp_servers;

CREATE POLICY mcp_servers_read_scope ON mcp_servers FOR SELECT USING (
    scope = 'system'
    OR current_setting('app.current_role', true) = 'super_admin'
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

CREATE POLICY mcp_servers_write_scope ON mcp_servers FOR INSERT WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) = 'tenant_admin'
    )
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

CREATE POLICY mcp_servers_update_scope ON mcp_servers FOR UPDATE USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) = 'tenant_admin'
    )
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) = 'tenant_admin'
    )
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

CREATE POLICY mcp_servers_delete_scope ON mcp_servers FOR DELETE USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) = 'tenant_admin'
    )
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);
