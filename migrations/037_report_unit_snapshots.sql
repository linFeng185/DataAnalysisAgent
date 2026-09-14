-- 报表单元执行快照：把"这一次取数到底发生了什么"完整落库，供反推排查。
-- 与 report_live_queries 的区别：后者只存 SQL 且无 unit_id 关联；本表按单元一条，
-- 覆盖提示词、模型、行数、截断原因与错误，支持"重跑这个单元"。
-- 与任务书的偏差：新增 user_id 列并采用 022 修正后的租户边界策略。
-- 原因：报表创建者角色可能是 analyst（quota.py 中 analyst 持有 report.create_page），
-- 仅管理员可写的策略会在 FORCE RLS 下把 analyst 的快照写入与读取全部静默拒绝。
CREATE TABLE IF NOT EXISTS report_unit_snapshots (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    report_id UUID NOT NULL,
    tenant_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    unit_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    datasource VARCHAR(64) NOT NULL DEFAULT '',
    dialect VARCHAR(32) NOT NULL DEFAULT '',
    sql_text TEXT NOT NULL DEFAULT '',
    prompt_text TEXT NOT NULL DEFAULT '',
    prompt_id VARCHAR(64) NOT NULL DEFAULT '',
    model_id VARCHAR(128) NOT NULL DEFAULT '',
    params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    row_count INTEGER NOT NULL DEFAULT 0,
    full_count INTEGER NOT NULL DEFAULT 0,
    truncated BOOLEAN NOT NULL DEFAULT FALSE,
    truncated_reason VARCHAR(200) NOT NULL DEFAULT '',
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    error_code VARCHAR(64) NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_report_unit_snapshots_report
    ON report_unit_snapshots (tenant_id, report_id, created_at DESC);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['report_unit_snapshots'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    END LOOP;
END $$;

DROP POLICY IF EXISTS report_unit_snapshots_scope ON report_unit_snapshots;
CREATE POLICY report_unit_snapshots_scope ON report_unit_snapshots FOR ALL USING (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
) WITH CHECK (
    current_setting('app.current_role', true) = 'super_admin'
    OR (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint
        AND (current_setting('app.current_role', true) = 'tenant_admin'
             OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint))
);
