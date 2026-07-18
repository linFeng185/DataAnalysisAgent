-- 行情数据表：Provider 成功返回后先写入，再交给上层分析。
CREATE TABLE IF NOT EXISTS market_bars (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL,
    market VARCHAR(32) NOT NULL DEFAULT 'cn_a',
    exchange VARCHAR(16) NOT NULL DEFAULT '',
    timestamp TIMESTAMPTZ NOT NULL,
    frequency VARCHAR(16) NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    amount DOUBLE PRECISION,
    provider VARCHAR(64) NOT NULL,
    adjustment VARCHAR(16) NOT NULL DEFAULT 'none',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_market_bars_identity UNIQUE (symbol, timestamp, frequency, adjustment, provider)
);

CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_frequency_timestamp
    ON market_bars (symbol, frequency, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_market_bars_market_exchange_timestamp
    ON market_bars (market, exchange, timestamp DESC);
