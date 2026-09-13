-- 报表计划后台生成（设计 48 第 7.2 节，需求 1.3/1.4/1.5/2.1）。
-- 018 已定稿不再修改：本文件按 pg_constraint 里的真实约束名删除旧 CHECK 后重建，
-- 避免内联约束名变化导致 DROP 失败并让整批迁移回滚。
-- SQLite 测试不执行本文件；ReportStore(dialect='sqlite'/'memory') 用应用层字段模拟。

-- 报表摘要标题：由 report.title 任务生成，模型不可用时回退为 prompt 前 20 字。
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS title VARCHAR(256) NOT NULL DEFAULT '';
-- 发散档位冗余列：报表信息列表直接出列，避免为每行解析 plan_json。
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS divergence VARCHAR(16) NOT NULL DEFAULT 'standard';
-- 创建时选定的数据源：同样供列表出列，后台规划任务也据此解析授权。
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS datasources_json JSONB NOT NULL DEFAULT '[]'::jsonb;
-- 计划失败的可读原因。report_jobs 没有错误列，plan_json 必须承载可执行计划，
-- 因此失败原因单独成列，列表与详情都能直接展示给用户。
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS plan_error TEXT NOT NULL DEFAULT '';

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    -- 只删除真正约束 status 列的 CHECK：按 pg_get_constraintdef 的内容筛选，
    -- 不假设 018 生成的名字仍是 report_jobs_status_check。
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'report_jobs'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE report_jobs DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

-- 新增 plan_failed：planning=生成中、draft=未生成（计划就绪）、plan_failed=计划失败。
ALTER TABLE report_jobs ADD CONSTRAINT report_jobs_status_check CHECK (
    status IN ('draft','planning','plan_failed','queued','running','succeeded','partial','failed','cancelled','expired','deleted')
);
ALTER TABLE report_jobs ADD CONSTRAINT report_jobs_divergence_check CHECK (
    divergence IN ('conservative','standard','deep')
);

-- 报表信息列表默认按创建时间倒序分页，索引覆盖「同租户 + 创建时间」的排序取数。
CREATE INDEX IF NOT EXISTS idx_report_jobs_created ON report_jobs (tenant_id, created_at DESC);
