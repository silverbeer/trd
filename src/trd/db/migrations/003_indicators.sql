-- The user's evolving list of followed indicators. See DESIGN.md "Indicator Data Model".
-- Add/remove an indicator = row change, zero code. Soft-disable keeps the learning history.
CREATE SEQUENCE IF NOT EXISTS indicator_config_id_seq;
CREATE TABLE IF NOT EXISTS indicator_config (
    id BIGINT PRIMARY KEY DEFAULT nextval('indicator_config_id_seq'),
    key TEXT NOT NULL,             -- matches the code registry: 'rsi', 'sma', ...
    params JSON NOT NULL,          -- {"period": 14} — overrides indicator defaults
    enabled BOOLEAN NOT NULL DEFAULT true,
    display_order INTEGER,
    note TEXT,                     -- learning journal: why added, what it tells me
    added_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    disabled_at TIMESTAMP
);
