CREATE SEQUENCE IF NOT EXISTS watchlist_id_seq;
CREATE TABLE IF NOT EXISTS watchlist (
    id BIGINT PRIMARY KEY DEFAULT nextval('watchlist_id_seq'),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS watchlist_item (
    watchlist_id BIGINT NOT NULL REFERENCES watchlist (id),
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    added_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (watchlist_id, instrument_id)
);

-- time_of_day (BMO/AMC) unavailable from yfinance; column reserved for a richer provider.
CREATE TABLE IF NOT EXISTS earnings_event (
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    date DATE NOT NULL,
    time_of_day TEXT,
    eps_estimate DECIMAL(18, 6),
    eps_actual DECIMAL(18, 6),
    PRIMARY KEY (instrument_id, date)
);
