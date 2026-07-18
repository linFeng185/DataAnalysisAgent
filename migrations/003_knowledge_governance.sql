-- 三范围知识、标签治理与数据库级访问控制（幂等）。

ALTER TABLE knowledge_files
    ADD COLUMN IF NOT EXISTS knowledge_scope VARCHAR(16) NOT NULL DEFAULT 'private';
ALTER TABLE knowledge_files
    ADD COLUMN IF NOT EXISTS datasource VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE knowledge_files
    ADD COLUMN IF NOT EXISTS tag_ids BIGINT[] NOT NULL DEFAULT '{}';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_knowledge_files_scope'
    ) THEN
        ALTER TABLE knowledge_files
            ADD CONSTRAINT ck_knowledge_files_scope
            CHECK (knowledge_scope IN ('system', 'tenant', 'private'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_knowledge_files_scope_identity_uploaded
    ON knowledge_files (knowledge_scope, tenant_id, user_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_files_datasource
    ON knowledge_files (datasource) WHERE datasource <> '';

CREATE TABLE IF NOT EXISTS knowledge_tags (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    slug VARCHAR(160) NOT NULL,
    tag_group VARCHAR(32) NOT NULL DEFAULT 'custom',
    aliases TEXT[] NOT NULL DEFAULT '{}',
    description TEXT NOT NULL DEFAULT '',
    scope VARCHAR(16) NOT NULL CHECK (scope IN ('global', 'private')),
    tenant_id INT NULL,
    owner_user_id INT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_seed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_knowledge_tags_owner CHECK (
        (scope = 'global' AND tenant_id IS NULL AND owner_user_id IS NULL)
        OR (scope = 'private' AND tenant_id IS NOT NULL AND owner_user_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_tags_global_slug
    ON knowledge_tags (slug) WHERE scope = 'global';
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_tags_private_slug
    ON knowledge_tags (tenant_id, owner_user_id, slug) WHERE scope = 'private';
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_search
    ON knowledge_tags (scope, tenant_id, owner_user_id, is_active, name);

INSERT INTO knowledge_tags (name, slug, tag_group, scope, is_seed) VALUES
    ('数据字典', '数据字典', 'knowledge_type', 'global', TRUE),
    ('表结构', '表结构', 'knowledge_type', 'global', TRUE),
    ('字段说明', '字段说明', 'knowledge_type', 'global', TRUE),
    ('指标口径', '指标口径', 'knowledge_type', 'global', TRUE),
    ('业务规则', '业务规则', 'knowledge_type', 'global', TRUE),
    ('枚举字典', '枚举字典', 'knowledge_type', 'global', TRUE),
    ('SQL模板', 'sql模板', 'knowledge_type', 'global', TRUE),
    ('数据质量', '数据质量', 'knowledge_type', 'global', TRUE),
    ('分析方法', '分析方法', 'knowledge_type', 'global', TRUE),
    ('报表模板', '报表模板', 'knowledge_type', 'global', TRUE),
    ('操作手册', '操作手册', 'knowledge_type', 'global', TRUE),
    ('故障排查', '故障排查', 'knowledge_type', 'global', TRUE),
    ('安全合规', '安全合规', 'knowledge_type', 'global', TRUE),
    ('产品文档', '产品文档', 'knowledge_type', 'global', TRUE),
    ('接口文档', '接口文档', 'knowledge_type', 'global', TRUE),
    ('MySQL', 'mysql', 'technology', 'global', TRUE),
    ('PostgreSQL', 'postgresql', 'technology', 'global', TRUE),
    ('ClickHouse', 'clickhouse', 'technology', 'global', TRUE),
    ('Oracle', 'oracle', 'technology', 'global', TRUE),
    ('SQL Server', 'sql-server', 'technology', 'global', TRUE),
    ('SQLite', 'sqlite', 'technology', 'global', TRUE)
ON CONFLICT (slug) WHERE scope = 'global' DO NOTHING;

-- 文件读取覆盖全局、当前租户和本人私有知识；写操作按角色进一步收紧。
ALTER TABLE knowledge_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_files FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS knowledge_file_identity_isolation ON knowledge_files;
DROP POLICY IF EXISTS knowledge_files_read_scope ON knowledge_files;
DROP POLICY IF EXISTS knowledge_files_insert_scope ON knowledge_files;
DROP POLICY IF EXISTS knowledge_files_update_scope ON knowledge_files;
DROP POLICY IF EXISTS knowledge_files_delete_scope ON knowledge_files;

CREATE POLICY knowledge_files_read_scope ON knowledge_files FOR SELECT USING (
    knowledge_scope = 'system'
    OR (
        knowledge_scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
    )
    OR (
        knowledge_scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
    OR current_setting('app.current_role', true) = 'super_admin'
);

CREATE POLICY knowledge_files_insert_scope ON knowledge_files FOR INSERT WITH CHECK (
    (
        knowledge_scope = 'system'
        AND current_setting('app.current_role', true) = 'super_admin'
    )
    OR (
        knowledge_scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) IN ('super_admin', 'tenant_admin')
    )
    OR (
        knowledge_scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

CREATE POLICY knowledge_files_update_scope ON knowledge_files FOR UPDATE USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        knowledge_scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) = 'tenant_admin'
    )
    OR (
        knowledge_scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        knowledge_scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) = 'tenant_admin'
    )
    OR (
        knowledge_scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

CREATE POLICY knowledge_files_delete_scope ON knowledge_files FOR DELETE USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        knowledge_scope = 'tenant'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND current_setting('app.current_role', true) = 'tenant_admin'
    )
    OR (
        knowledge_scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

-- 标签读取只暴露全局和本人标签；平台管理员可治理全部标签。
ALTER TABLE knowledge_tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_tags FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS knowledge_tags_read_scope ON knowledge_tags;
DROP POLICY IF EXISTS knowledge_tags_insert_scope ON knowledge_tags;
DROP POLICY IF EXISTS knowledge_tags_update_scope ON knowledge_tags;

CREATE POLICY knowledge_tags_read_scope ON knowledge_tags FOR SELECT USING (
    scope = 'global'
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
    OR current_setting('app.current_role', true) = 'super_admin'
);

CREATE POLICY knowledge_tags_insert_scope ON knowledge_tags FOR INSERT WITH CHECK (
    (
        scope = 'global'
        AND current_setting('app.current_role', true) = 'super_admin'
    )
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);

CREATE POLICY knowledge_tags_update_scope ON knowledge_tags FOR UPDATE USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (
        scope = 'private'
        AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::int
        AND owner_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
    )
);
