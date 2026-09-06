CREATE TABLE platform_runtime_settings (
    id integer PRIMARY KEY CHECK (id=1),
    revision bigint NOT NULL DEFAULT 0,
    values_json jsonb NOT NULL DEFAULT '{}',
    plugins_json jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT NOW()
);
INSERT INTO platform_runtime_settings(id) VALUES (1);
CREATE TABLE platform_runtime_audit (
    id bigserial PRIMARY KEY,
    actor_user_id bigint NOT NULL,
    action text NOT NULL,
    keys jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT NOW()
);
ALTER TABLE platform_runtime_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_runtime_settings FORCE ROW LEVEL SECURITY;
ALTER TABLE platform_runtime_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_runtime_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY platform_settings_admin ON platform_runtime_settings FOR ALL USING (current_setting('app.current_role',true)='super_admin');
CREATE POLICY platform_settings_audit ON platform_runtime_audit FOR ALL USING (current_setting('app.current_role',true)='super_admin');
