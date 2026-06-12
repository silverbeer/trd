-- Generalize the simulation machinery into contribution plans: a recurring
-- monthly investment attached to ANY account (real or simulation). Real-account
-- plans record buys the user executed at their broker; sim-account plans are
-- paper. Plan transactions carry plan_id so a plan's performance is tracked
-- separately from other holdings in the same account.

CREATE SEQUENCE IF NOT EXISTS contribution_plan_id_seq;
CREATE TABLE IF NOT EXISTS contribution_plan (
    id BIGINT PRIMARY KEY DEFAULT nextval('contribution_plan_id_seq'),
    account_id BIGINT NOT NULL UNIQUE REFERENCES account (id),
    monthly_amount DECIMAL(18, 2) NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('ticker', 'momentum', 'allocation')),
    strategy_ticker TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS plan_allocation (
    plan_id BIGINT NOT NULL REFERENCES contribution_plan (id),
    symbol TEXT NOT NULL,
    weight DECIMAL(7, 4) NOT NULL CHECK (weight > 0),
    PRIMARY KEY (plan_id, symbol)
);

ALTER TABLE txn ADD COLUMN IF NOT EXISTS plan_id BIGINT;

-- Carry over existing sim configs and tag their transactions (sim accounts are
-- plan-only, so every txn in a sim account belongs to its plan).
INSERT INTO contribution_plan (account_id, monthly_amount, strategy, strategy_ticker, created_at)
SELECT account_id, monthly_amount, strategy, strategy_ticker, created_at FROM sim_config;

INSERT INTO plan_allocation (plan_id, symbol, weight)
SELECT p.id, sa.symbol, sa.weight
FROM sim_allocation sa
JOIN contribution_plan p ON p.account_id = sa.account_id;

UPDATE txn SET plan_id = (
    SELECT p.id FROM contribution_plan p WHERE p.account_id = txn.account_id
)
WHERE account_id IN (SELECT account_id FROM contribution_plan);

DROP TABLE sim_allocation;
DROP TABLE sim_config;
