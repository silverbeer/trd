-- Live engine phase 2: the guardrails that have to exist before trade approvals
-- are switched off (SB-1377).
--
-- A kill switch and four hard caps, enforced by trd independently of the
-- strategy config: the most one order may cost, the most the engine may buy in
-- one session, the most positions it may hold, and the loss at which it halts
-- itself.
--
-- Its own table, keyed by account, rather than columns on engine_config: config
-- rows are deleted and re-inserted on every reconfigure (EngineConfigRepo.upsert,
-- which `engine cash --set` goes through), and a kill switch that a capital change
-- quietly released would be worse than none. The account is the engine's stable
-- identity — the same reason order_intent hangs off it.
--
-- No row is not "no caps". A live engine with no row (every one created before
-- this migration) is read with the defaults `init --live` would have written,
-- derived in code from its config (trd.services.guard), so the constants live in
-- one place and the engine already trading is capped the moment this deploys.
-- A row is written when someone halts, resumes or sets a cap.
--
-- halted_by: 'manual' (trd engine halt) or 'max_loss' (the loss stop tripped).
-- Validated in code, no CHECK — DuckDB can never widen one (see 027).
CREATE TABLE IF NOT EXISTS engine_guard (
    account_id BIGINT PRIMARY KEY REFERENCES account (id),
    halted BOOLEAN NOT NULL DEFAULT FALSE,
    halt_reason TEXT,
    halted_at TIMESTAMP,
    halted_by TEXT,
    max_order DECIMAL(18, 6),        -- dollars, one buy order's notional
    max_daily DECIMAL(18, 6),        -- dollars, buy notional per session
    max_positions INTEGER,           -- open positions + open buy intents
    max_loss DECIMAL(18, 6),         -- dollars, realized + unrealized floor
    updated_at TIMESTAMP
);
