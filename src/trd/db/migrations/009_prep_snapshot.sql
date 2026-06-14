-- Sunday Prep history. One row per briefing: denormalized columns for fast trend
-- queries (how has the VIX regime / sector leadership / breadth drifted week over
-- week) plus the full briefing as JSON for anything else. We persist only what can't
-- be recomputed from txn + price_daily — the point-in-time market environment.
CREATE SEQUENCE IF NOT EXISTS prep_snapshot_id_seq;
CREATE TABLE IF NOT EXISTS prep_snapshot (
    id BIGINT PRIMARY KEY DEFAULT nextval('prep_snapshot_id_seq'),
    snapshot_date DATE NOT NULL UNIQUE,   -- reference date the briefing was built for
    generated_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    vix DECIMAL(10, 2),
    vix_band TEXT,
    avg_futures_pct DECIMAL(10, 4),       -- mean futures move = a breadth/tone proxy
    top_sector TEXT,
    top_sector_pct DECIMAL(10, 4),
    worst_sector TEXT,
    worst_sector_pct DECIMAL(10, 4),
    fomc_week BOOLEAN NOT NULL DEFAULT FALSE,
    earnings_count INTEGER NOT NULL DEFAULT 0,
    payload JSON NOT NULL
);
