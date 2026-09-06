-- 持久化暂停状态和运行配额持有信息，恢复时精确释放原周期并发槽。
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS is_paused BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS quota_period_start TIMESTAMPTZ;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS quota_held BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_report_jobs_recovery ON report_jobs (updated_at) WHERE status='running';
