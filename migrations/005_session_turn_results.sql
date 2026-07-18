-- 会话历史逐轮结构化响应持久化。

ALTER TABLE query_history
    ADD COLUMN IF NOT EXISTS final_result JSONB NOT NULL DEFAULT '{}'::jsonb;
