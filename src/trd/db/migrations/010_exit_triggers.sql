-- Exit triggers: a stop and/or target level tied to a holding (account + instrument).
-- One trigger per (account, instrument); `trd exit set` upserts it. The rule is
-- evaluated against the latest daily close (not intraday) — "exit on a close beyond X".
CREATE SEQUENCE IF NOT EXISTS exit_trigger_id_seq;
CREATE TABLE IF NOT EXISTS exit_trigger (
    id BIGINT PRIMARY KEY DEFAULT nextval('exit_trigger_id_seq'),
    account_id BIGINT NOT NULL REFERENCES account (id),
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    stop_price DECIMAL(24, 8),
    target_price DECIMAL(24, 8),
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    UNIQUE (account_id, instrument_id)
);
