-- Give the engine a timeframe, and key its signals to a bar instant.
--
-- `timeframe` is the bar width the rules run on: '1d' for a swing engine (every
-- engine that exists today), '5m'/'15m' for a day engine reading price_intraday.
-- Defaulting to '1d' is what makes this migration a no-op for a running engine.
--
-- The signal key has to widen with it. `UNIQUE (instrument_id, strategy,
-- bar_date)` exists so a monitor loop re-firing every 60 seconds records a
-- signal once per bar rather than once per pass — correct when a bar *is* a day,
-- but on a 5-minute series it collapses a whole session into one row: the first
-- 09:35 signal is stored and the 78 buckets after it are silently dropped, which
-- is exactly the audit trail 'trd engine signals' exists to keep.
--
-- DuckDB cannot drop a constraint and cannot drop a table another table
-- references, so both engine tables are rebuilt: stage the rows out, drop
-- position (the referrer) then signal, recreate both with the new key, copy
-- back. The id sequences are untouched, so ids and every signal->position link
-- survive. Daily signals become midnight timestamps, which is what a daily bar's
-- instant is.
CREATE TABLE engine_signal_old AS SELECT * FROM engine_signal;
CREATE TABLE engine_position_old AS SELECT * FROM engine_position;

DROP TABLE engine_position;
DROP TABLE engine_signal;

CREATE TABLE engine_signal (
    id BIGINT PRIMARY KEY DEFAULT nextval('engine_signal_id_seq'),
    run_id BIGINT REFERENCES engine_run (id),
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    strategy TEXT NOT NULL,
    bar_ts TIMESTAMP NOT NULL,   -- the bar's opening instant; midnight for a daily bar
    fired_at TIMESTAMP NOT NULL,
    price DECIMAL(24, 8) NOT NULL,
    score DOUBLE NOT NULL,
    reason TEXT NOT NULL,
    acted BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (instrument_id, strategy, bar_ts)
);

INSERT INTO engine_signal
    SELECT id, run_id, instrument_id, strategy, CAST(bar_date AS TIMESTAMP),
           fired_at, price, score, reason, acted
    FROM engine_signal_old;

CREATE TABLE engine_position (
    id BIGINT PRIMARY KEY DEFAULT nextval('engine_position_id_seq'),
    account_id BIGINT NOT NULL REFERENCES account (id),
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    signal_id BIGINT REFERENCES engine_signal (id),
    strategy TEXT NOT NULL,
    opened_at TIMESTAMP NOT NULL,
    entry_price DECIMAL(24, 8) NOT NULL,
    quantity DECIMAL(24, 8) NOT NULL,
    stop_price DECIMAL(24, 8) NOT NULL,
    target_price DECIMAL(24, 8) NOT NULL,
    atr_at_entry DECIMAL(24, 8) NOT NULL,
    trail_high DECIMAL(24, 8) NOT NULL,
    bars_held INTEGER NOT NULL DEFAULT 0,
    last_bar_date DATE,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TIMESTAMP,
    exit_price DECIMAL(24, 8),
    exit_reason TEXT
);

INSERT INTO engine_position SELECT * FROM engine_position_old;

DROP TABLE engine_position_old;
DROP TABLE engine_signal_old;

-- Added bare: DuckDB rejects ADD COLUMN with a constraint, so the column is
-- nullable and the repo reads NULL as '1d' — the timeframe every engine written
-- before this migration has always run on.
ALTER TABLE engine_config ADD COLUMN timeframe TEXT;
UPDATE engine_config SET timeframe = '1d' WHERE timeframe IS NULL;
