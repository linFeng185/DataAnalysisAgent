-- 租户编码登录、租户用户自治与租户级 LLM 治理。

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS code VARCHAR(32);
UPDATE tenants SET code = 'default' WHERE id = 1 AND (code IS NULL OR code = '');
UPDATE tenants SET code = 'tenant-' || id::text WHERE id <> 1 AND (code IS NULL OR code = '');
ALTER TABLE tenants ALTER COLUMN code SET NOT NULL;
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_code_format;
ALTER TABLE tenants ADD CONSTRAINT ck_tenants_code_format CHECK (
    code = LOWER(code) AND code ~ '^[a-z0-9][a-z0-9-]{0,31}$'
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_code ON tenants (LOWER(code));

CREATE OR REPLACE FUNCTION prevent_tenant_code_change()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.code IS DISTINCT FROM OLD.code THEN
        RAISE EXCEPTION 'tenant code is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_tenant_code_change ON tenants;
CREATE TRIGGER trg_prevent_tenant_code_change
BEFORE UPDATE OF code ON tenants
FOR EACH ROW EXECUTE FUNCTION prevent_tenant_code_change();

DROP INDEX IF EXISTS uq_users_username_global;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_users_tenant_username'
    ) THEN
        ALTER TABLE users ADD CONSTRAINT uq_users_tenant_username
            UNIQUE (tenant_id, username);
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS llm_provider_catalog (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(64) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    protocol VARCHAR(32) NOT NULL,
    default_base_url TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_llm_provider_protocol CHECK (protocol IN ('openai_compatible', 'anthropic'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_provider_code
    ON llm_provider_catalog (LOWER(code));

CREATE TABLE IF NOT EXISTS llm_model_catalog (
    id BIGSERIAL PRIMARY KEY,
    provider_id BIGINT NOT NULL REFERENCES llm_provider_catalog(id),
    model_id VARCHAR(128) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider_id, model_id)
);

CREATE TABLE IF NOT EXISTS tenant_llm_connections (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    provider_id BIGINT NOT NULL REFERENCES llm_provider_catalog(id),
    name VARCHAR(128) NOT NULL,
    base_url TEXT NOT NULL DEFAULT '',
    encrypted_api_key TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_tenant_llm_connections_tenant
    ON tenant_llm_connections (tenant_id, is_active);

CREATE TABLE IF NOT EXISTS tenant_llm_connection_models (
    connection_id BIGINT NOT NULL REFERENCES tenant_llm_connections(id) ON DELETE CASCADE,
    model_catalog_id BIGINT NOT NULL REFERENCES llm_model_catalog(id),
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (connection_id, model_catalog_id)
);

CREATE TABLE IF NOT EXISTS tenant_llm_defaults (
    tenant_id INT PRIMARY KEY REFERENCES tenants(id),
    connection_id BIGINT NOT NULL REFERENCES tenant_llm_connections(id) ON DELETE CASCADE,
    model_catalog_id BIGINT NOT NULL REFERENCES llm_model_catalog(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO llm_provider_catalog (code, display_name, protocol, default_base_url)
VALUES
    ('openai', 'OpenAI', 'openai_compatible', 'https://api.openai.com/v1'),
    ('anthropic', 'Anthropic', 'anthropic', 'https://api.anthropic.com'),
    ('deepseek', 'DeepSeek', 'openai_compatible', 'https://api.deepseek.com/v1')
ON CONFLICT DO NOTHING;

INSERT INTO llm_model_catalog (provider_id, model_id, display_name, capabilities)
SELECT id, 'gpt-4o', 'GPT-4o',
       '{"streaming":true,"reasoning":true,"vision":true,"context_window":128000}'::jsonb
FROM llm_provider_catalog WHERE code = 'openai'
ON CONFLICT DO NOTHING;

INSERT INTO llm_model_catalog (provider_id, model_id, display_name, capabilities)
SELECT id, 'claude-sonnet-4-6', 'Claude Sonnet 4.6',
       '{"streaming":true,"reasoning":true,"vision":true,"context_window":200000}'::jsonb
FROM llm_provider_catalog WHERE code = 'anthropic'
ON CONFLICT DO NOTHING;

INSERT INTO llm_model_catalog (provider_id, model_id, display_name, capabilities)
SELECT id, 'deepseek-chat', 'DeepSeek Chat',
       '{"streaming":true,"reasoning":false,"vision":false,"context_window":128000}'::jsonb
FROM llm_provider_catalog WHERE code = 'deepseek'
ON CONFLICT DO NOTHING;
