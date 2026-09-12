-- 029：报表运行真实计数（覆盖 23.21）。
--
-- 背景：report_runs 既有的 query_count 与 token_usage 由 runner 写入
-- plan.estimated 的**计划阶段估算值**，不是实际发生值。本次新增五列真实计数，
-- 并把 token_usage 的写入改为真实 token 数（该列此前只用于展示，无配额消费者）。
--
-- 语义边界：
--   * query_count 保持**配额计费口径的估算值**不变，配额预占与释放依赖它；
--   * 新增的 sql_query_count 是真实提交执行次数，两者并存以便对比预估与实际；
--   * sql_success_count + sql_failure_count = sql_query_count；
--   * llm_call_count 含失败次数，成功数不单独存列，由两者相减得到。

ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS llm_call_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS llm_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS sql_query_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS sql_success_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS sql_failure_count INTEGER NOT NULL DEFAULT 0;
