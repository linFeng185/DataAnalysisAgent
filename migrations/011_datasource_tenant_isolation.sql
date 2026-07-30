-- 数据源名称只在租户内唯一，避免同名配置跨租户覆盖。
DO $$
BEGIN
    ALTER TABLE datasource_configs
        DROP CONSTRAINT IF EXISTS datasource_configs_name_key;
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_datasource_configs_tenant_name
    ON datasource_configs (tenant_id, name);
