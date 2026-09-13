-- 031：表结构同步排队（覆盖需求 8）。
--
-- 背景：026 建立的 schema_sync_jobs 只有运行中的活动状态，服务在并发满时直接
-- 返回 429 拒绝启动，用户看到的是「任务中断」而不是「稍后自动执行」。
--
-- 本次改动：
--   * status 枚举新增 'queued'：并发已满时把启动请求持久化为排队行；
--   * 排队行不持有 advisory lock，进程重启或换 Worker 后仍可被调度循环认领；
--   * 语义边界：'queued' 既不是运行中也不是终态，崩溃回收（无主任务探测）
--     只处理 running/cancelling，绝不把排队行误判为 failed。
--
-- 约束名说明：026 用内联 CHECK 建表，PostgreSQL 自动命名为
-- schema_sync_jobs_status_check；这里按实际约束名删除后重建，避免出现
-- 两个并存的 status 约束导致新状态仍被旧约束拒绝。

DO $$
DECLARE
    existing_name text;
BEGIN
    SELECT conname INTO existing_name
    FROM pg_constraint
    WHERE conrelid = 'schema_sync_jobs'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%status%';
    IF existing_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE schema_sync_jobs DROP CONSTRAINT %I', existing_name);
    END IF;
END $$;

ALTER TABLE schema_sync_jobs ADD CONSTRAINT schema_sync_jobs_status_check CHECK (
    status IN ('queued','running','cancelling','succeeded','cancelled','failed')
);

-- 调度循环按 started_at 升序认领排队行，部分索引让扫描与队列位置统计不随历史行增长。
CREATE INDEX IF NOT EXISTS idx_schema_sync_jobs_queued
    ON schema_sync_jobs (started_at, datasource)
    WHERE status = 'queued';
