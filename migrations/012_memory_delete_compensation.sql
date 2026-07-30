-- 删除补偿需要在主记录已删除后继续保留 tombstone，故移除外键级联约束。
ALTER TABLE pending_vector_sync
    DROP CONSTRAINT IF EXISTS pending_vector_sync_entry_id_fkey;
