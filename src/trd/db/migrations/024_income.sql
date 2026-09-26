-- Cash a holding pays you: dividends, interest, a broker's cash sweep.
--
-- Until this table, trd could not record any of it. `txn.side` is buy or sell
-- and nothing else, so every return the tracker reported — equity curve,
-- dashboard XIRR, a DCA plan's showing against SPY — measured price
-- appreciation alone and silently understated the truth.
--
-- Small money that becomes large money. On a dividend-paying core held for a
-- decade, reinvested distributions are a substantial share of total return, and
-- the plans exist to answer "am I beating the index" — the one comparison this
-- gap corrupts, because a price-only return on one side is not the same claim
-- as a total return on the other.
--
-- Deliberately NOT a third value on `txn.side`. That enum is load-bearing in
-- FIFO: a new member would reach every branch matching on buy/sell, and a
-- dividend creates no lot and consumes none. Kept apart, the holdings
-- arithmetic cannot be touched by it at all.
--
-- What belongs here is cash that ARRIVED as cash. A reinvested dividend (DRIP)
-- is economically a purchase — it creates a real lot with a real cost basis —
-- and stays an ordinary `txn` buy. Recording it in both places would double it.
CREATE SEQUENCE IF NOT EXISTS income_id_seq;

CREATE TABLE IF NOT EXISTS income (
    id BIGINT PRIMARY KEY DEFAULT nextval('income_id_seq'),
    account_id BIGINT NOT NULL REFERENCES account (id),
    -- NULL for account-level cash: interest, a sweep payment. Anything paid BY
    -- a holding names it, so income can be read per position.
    instrument_id BIGINT REFERENCES instrument (id),
    -- dividend | interest | cash_sweep, validated in the model rather than by a
    -- CHECK. `instrument.type` carries a CHECK from 001_init that DuckDB cannot
    -- widen, with twelve foreign keys pointing at the table — a constraint that
    -- has already cost a migration here. A kind added later must not need one.
    kind TEXT NOT NULL,
    amount DECIMAL(18, 8) NOT NULL,
    received_at TIMESTAMP NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_income_account ON income (account_id, received_at);
