-- API 访问策略与接口级 IP 黑白名单。

CREATE TABLE IF NOT EXISTS api_access_policies (
    id BIGSERIAL PRIMARY KEY,
    policy_key VARCHAR(64) NOT NULL UNIQUE,
    path VARCHAR(512) NOT NULL,
    path_type VARCHAR(16) NOT NULL DEFAULT 'exact',
    methods TEXT[] NOT NULL,
    auth_mode VARCHAR(32) NOT NULL,
    access_log_mode VARCHAR(16) NOT NULL DEFAULT 'standard',
    priority INT NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT NOT NULL DEFAULT '',
    created_by INT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_api_policy_key CHECK (policy_key ~ '^[a-z][a-z0-9_-]+$'),
    CONSTRAINT ck_api_policy_path CHECK (LEFT(path, 1) = '/'),
    CONSTRAINT ck_api_policy_path_type CHECK (path_type IN ('exact', 'template')),
    CONSTRAINT ck_api_policy_methods CHECK (CARDINALITY(methods) > 0),
    CONSTRAINT ck_api_policy_auth CHECK (
        auth_mode IN ('jwt', 'jwt_or_admin_key', 'super_admin')
    ),
    CONSTRAINT ck_api_policy_log CHECK (
        access_log_mode IN ('standard', 'security', 'audit', 'none')
    )
);

CREATE INDEX IF NOT EXISTS ix_api_access_policies_match
    ON api_access_policies (enabled, priority DESC, id);

CREATE TABLE IF NOT EXISTS api_ip_rules (
    id BIGSERIAL PRIMARY KEY,
    policy_key VARCHAR(64) NOT NULL,
    action VARCHAR(8) NOT NULL,
    cidr CIDR NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT NOT NULL DEFAULT '',
    created_by INT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_api_ip_rule_action CHECK (action IN ('allow', 'deny')),
    UNIQUE (policy_key, action, cidr)
);

CREATE INDEX IF NOT EXISTS ix_api_ip_rules_policy
    ON api_ip_rules (policy_key, enabled, action);

ALTER TABLE api_access_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_access_policies FORCE ROW LEVEL SECURITY;
ALTER TABLE api_ip_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_ip_rules FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS api_access_policies_super_admin ON api_access_policies;
CREATE POLICY api_access_policies_super_admin ON api_access_policies
    USING (
        NULLIF(current_setting('app.current_user_id', true), '')::int = 1
        AND current_setting('app.current_role', true) = 'super_admin'
    )
    WITH CHECK (
        NULLIF(current_setting('app.current_user_id', true), '')::int = 1
        AND current_setting('app.current_role', true) = 'super_admin'
    );

DROP POLICY IF EXISTS api_ip_rules_super_admin ON api_ip_rules;
CREATE POLICY api_ip_rules_super_admin ON api_ip_rules
    USING (
        NULLIF(current_setting('app.current_user_id', true), '')::int = 1
        AND current_setting('app.current_role', true) = 'super_admin'
    )
    WITH CHECK (
        NULLIF(current_setting('app.current_user_id', true), '')::int = 1
        AND current_setting('app.current_role', true) = 'super_admin'
    );
