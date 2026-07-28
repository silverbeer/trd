-- Trading engine: monitor-mode paper trading.
--
-- The engine scans a small universe (a watchlist), runs entry strategies against
-- stored daily bars with the live quote as the forming bar, and manages virtual
-- positions inside a *simulation* account. Fills are recorded as ordinary txn
-- rows, so FIFO / portfolio / equity / XIRR all work on the engine account for
-- free. These tables hold only what txn cannot express: which strategy fired,
-- why, and the exit state machine for each open trade.

-- One config row per engine account.
CREATE SEQUENCE IF NOT EXISTS engine_config_id_seq;
CREATE TABLE IF NOT EXISTS engine_config (
    id BIGINT PRIMARY KEY DEFAULT nextval('engine_config_id_seq'),
    account_id BIGINT NOT NULL REFERENCES account (id),
    watchlist TEXT NOT NULL,
    position_size DECIMAL(24, 8) NOT NULL,   -- dollars committed per trade
    max_positions INTEGER NOT NULL,
    strategies TEXT NOT NULL,                -- JSON array of enabled strategy keys
    exit_params TEXT NOT NULL,               -- JSON object of exit-rule parameters
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    UNIQUE (account_id)
);

-- One row per scan pass. The audit trail for "what did the engine see, and when".
CREATE SEQUENCE IF NOT EXISTS engine_run_id_seq;
CREATE TABLE IF NOT EXISTS engine_run (
    id BIGINT PRIMARY KEY DEFAULT nextval('engine_run_id_seq'),
    started_at TIMESTAMP NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    signals INTEGER NOT NULL DEFAULT 0,
    opened INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0,
    paper BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT
);

-- Every entry signal ever fired, acted on or not. One per (symbol, strategy, bar)
-- so a 60-second monitor loop re-firing all day stores the signal exactly once.
CREATE SEQUENCE IF NOT EXISTS engine_signal_id_seq;
CREATE TABLE IF NOT EXISTS engine_signal (
    id BIGINT PRIMARY KEY DEFAULT nextval('engine_signal_id_seq'),
    run_id BIGINT REFERENCES engine_run (id),
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    strategy TEXT NOT NULL,
    bar_date DATE NOT NULL,
    fired_at TIMESTAMP NOT NULL,
    price DECIMAL(24, 8) NOT NULL,
    score DOUBLE NOT NULL,
    reason TEXT NOT NULL,
    acted BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (instrument_id, strategy, bar_date)
);

-- A virtual position and its exit state. stop_price is the *initial* stop and is
-- never mutated — the trailing stop is derived from trail_high — so the R-multiple
-- of a closed trade stays well defined.
CREATE SEQUENCE IF NOT EXISTS engine_position_id_seq;
CREATE TABLE IF NOT EXISTS engine_position (
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
