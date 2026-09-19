-- 报表运行 token 用量拆分：原来只有 token_usage 一个总数，执行明细页看不到
-- "用了多少输入、多少输出 token"。上游 _extract_token_usage 本来就解析出了拆分，
-- 只是记录时被加总丢弃，这里补上落库列。
ALTER TABLE report_runs
    ADD COLUMN IF NOT EXISTS prompt_token_usage BIGINT NOT NULL DEFAULT 0;
ALTER TABLE report_runs
    ADD COLUMN IF NOT EXISTS completion_token_usage BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN report_runs.prompt_token_usage IS '本次运行消耗的输入（prompt）token 用量';
COMMENT ON COLUMN report_runs.completion_token_usage IS '本次运行消耗的输出（completion）token 用量';
