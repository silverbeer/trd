-- Simulation account settings. The sim account itself is a normal account row
-- (type 'simulation') — same transaction/FIFO machinery as real money.
CREATE TABLE IF NOT EXISTS sim_config (
    account_id BIGINT PRIMARY KEY REFERENCES account (id),
    monthly_amount DECIMAL(18, 2) NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('ticker', 'momentum')),
    strategy_ticker TEXT,          -- for the 'ticker' strategy
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);
