-- Money moved into or out of an account, and a staged allocation plan for it
-- (SB-1383: the Fidelity Rollover IRA).
--
-- cash_flow. Until this table trd knew only trades and income, so an account's
-- cash was invisible: a $250,930 rollover that lands as cash and is invested
-- over twelve months read as an account worth only what had been bought so far,
-- and every return measured from the first buy instead of from the day the money
-- arrived. A deposit is not a trade — it creates no lot — so, like income, it is
-- its own table and never a value on `txn.side`, which FIFO matches on.
--
-- kind is deposit | withdrawal, validated in the model rather than by a CHECK:
-- DuckDB cannot widen a CHECK later, and a rollover in, a transfer out and a
-- distribution are kinds this may grow. amount is always positive; the kind
-- carries the direction.
CREATE SEQUENCE IF NOT EXISTS cash_flow_id_seq;

CREATE TABLE IF NOT EXISTS cash_flow (
    id BIGINT PRIMARY KEY DEFAULT nextval('cash_flow_id_seq'),
    account_id BIGINT NOT NULL REFERENCES account (id),
    kind TEXT NOT NULL,
    amount DECIMAL(18, 2) NOT NULL,
    occurred_at TIMESTAMP NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_cash_flow_account ON cash_flow (account_id, occurred_at);

-- alloc_plan: one per account. What the money is FOR — target weights, how much
-- goes in at the start and how much later, and when. A plan, never an order:
-- whether a milestone happened is read from the buys recorded against the
-- account, not from anything stored here.
--
-- account_kind / brokerage live here, not on `account`: `account.type` has a
-- CHECK from 001_init that DuckDB cannot widen, and a column added to a table
-- twelve foreign keys point at is the migration that has already failed once.
CREATE SEQUENCE IF NOT EXISTS alloc_plan_id_seq;

CREATE TABLE IF NOT EXISTS alloc_plan (
    id BIGINT PRIMARY KEY DEFAULT nextval('alloc_plan_id_seq'),
    account_id BIGINT NOT NULL UNIQUE REFERENCES account (id),
    name TEXT NOT NULL,
    account_kind TEXT,
    brokerage TEXT,
    capital DECIMAL(18, 2) NOT NULL,
    drift_threshold DECIMAL(6, 2) NOT NULL DEFAULT 5,
    created_at TIMESTAMP NOT NULL,
    note TEXT
);

-- Target weights. symbol 'CASH' means: hold this share uninvested. A Treasury
-- fund held for the same purpose is its own ticker, priced and drifting like any
-- other — cash and Treasuries are not the same asset.
CREATE TABLE IF NOT EXISTS alloc_target (
    plan_id BIGINT NOT NULL REFERENCES alloc_plan (id),
    symbol TEXT NOT NULL,
    weight DECIMAL(7, 4) NOT NULL,
    label TEXT,
    PRIMARY KEY (plan_id, symbol)
);

-- The deployment schedule. seq 0 is the initial tranche. status is the person's
-- decision about a milestone (planned | postponed | cancelled); whether it is
-- done, partly done or overdue is DERIVED from the account's recorded buys, so it
-- can never claim money was invested that was not.
CREATE SEQUENCE IF NOT EXISTS alloc_milestone_id_seq;

CREATE TABLE IF NOT EXISTS alloc_milestone (
    id BIGINT PRIMARY KEY DEFAULT nextval('alloc_milestone_id_seq'),
    plan_id BIGINT NOT NULL REFERENCES alloc_plan (id),
    seq INTEGER NOT NULL,
    due_date DATE NOT NULL,
    amount DECIMAL(18, 2) NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    note TEXT
);

-- Every material change to a plan, in words: a re-target, a postponed
-- milestone. The plan's history, so "why is it 45% IVV now" has an answer.
CREATE SEQUENCE IF NOT EXISTS alloc_event_id_seq;

CREATE TABLE IF NOT EXISTS alloc_event (
    id BIGINT PRIMARY KEY DEFAULT nextval('alloc_event_id_seq'),
    plan_id BIGINT NOT NULL REFERENCES alloc_plan (id),
    logged_at TIMESTAMP NOT NULL,
    what TEXT NOT NULL
);
