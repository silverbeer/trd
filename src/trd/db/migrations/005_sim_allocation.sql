-- Add the 'allocation' strategy: split the monthly contribution across multiple
-- tickers by weight (e.g. 30% SPY / 70% QQQ). CHECK constraints can't be altered
-- in place, so rebuild sim_config with the widened constraint.
CREATE TABLE sim_config_new (
    account_id BIGINT PRIMARY KEY REFERENCES account (id),
    monthly_amount DECIMAL(18, 2) NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('ticker', 'momentum', 'allocation')),
    strategy_ticker TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);
INSERT INTO sim_config_new SELECT * FROM sim_config;
DROP TABLE sim_config;
ALTER TABLE sim_config_new RENAME TO sim_config;

-- Weights for the 'allocation' strategy, in percent. Rows for one account sum to 100.
CREATE TABLE IF NOT EXISTS sim_allocation (
    account_id BIGINT NOT NULL REFERENCES account (id),
    symbol TEXT NOT NULL,
    weight DECIMAL(7, 4) NOT NULL CHECK (weight > 0),
    PRIMARY KEY (account_id, symbol)
);
