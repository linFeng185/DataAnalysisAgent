-- 为知识库自进化晋升增加数据库级 Claim，支持多 Worker 幂等和故障重试。

ALTER TABLE knowledge_sql_observations
    ADD COLUMN IF NOT EXISTS promotion_claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS promotion_claim_token TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS memory_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS last_promotion_error TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_knowledge_sql_observations_promotion_claim
    ON knowledge_sql_observations (promotion_claimed_at)
    WHERE promoted_at IS NULL;
