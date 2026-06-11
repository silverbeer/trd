CREATE SEQUENCE IF NOT EXISTS instrument_id_seq;
CREATE TABLE IF NOT EXISTS instrument (
    id BIGINT PRIMARY KEY DEFAULT nextval('instrument_id_seq'),
    symbol TEXT NOT NULL UNIQUE,
    name TEXT,
    type TEXT NOT NULL CHECK (type IN ('stock', 'etf', 'crypto')),
    exchange TEXT,
    sector TEXT,
    currency TEXT NOT NULL DEFAULT 'USD',
    added_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE SEQUENCE IF NOT EXISTS account_id_seq;
CREATE TABLE IF NOT EXISTS account (
    id BIGINT PRIMARY KEY DEFAULT nextval('account_id_seq'),
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK (type IN ('real', 'simulation')),
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- 'transaction' is a reserved word; table is txn, domain model is Transaction.
CREATE SEQUENCE IF NOT EXISTS txn_id_seq;
CREATE TABLE IF NOT EXISTS txn (
    id BIGINT PRIMARY KEY DEFAULT nextval('txn_id_seq'),
    account_id BIGINT NOT NULL REFERENCES account (id),
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity DECIMAL(24, 8) NOT NULL CHECK (quantity > 0),
    price DECIMAL(24, 8) NOT NULL CHECK (price >= 0),
    fees DECIMAL(24, 8) NOT NULL DEFAULT 0,
    executed_at TIMESTAMP NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS price_daily (
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    date DATE NOT NULL,
    open DECIMAL(24, 8),
    high DECIMAL(24, 8),
    low DECIMAL(24, 8),
    close DECIMAL(24, 8) NOT NULL,
    volume BIGINT,
    PRIMARY KEY (instrument_id, date)
);

CREATE TABLE IF NOT EXISTS quote_snapshot (
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    price DECIMAL(24, 8) NOT NULL,
    prev_close DECIMAL(24, 8),
    captured_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);
