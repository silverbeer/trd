-- Live engine mode, phase 1a: the engine records what it WANTS to trade.
--
-- A paper engine fills its own decisions: a scan writes a txn and opens an
-- engine_position in a simulation account. A live engine cannot. The fill is the
-- broker's, it arrives later, at a price the scan did not see, and sometimes not
-- at all. So a live scan writes an order intent and stops there; phase 1b ingests
-- the fill (txn + position, at the real price) and phase 1c is the outbox that
-- hands intents to the broker session. Nothing under src/trd ever talks to the
-- broker itself.
--
-- engine_config.mode
--   'paper' (and NULL, every engine that predates this column) or 'live'.
--   Validated in code by EngineMode, not by a CHECK: account.type's CHECK from
--   001_init is the standing example of a constraint DuckDB can never widen, which
--   is why a live engine is a mode on the config and not a third account type.
--   Added bare: DuckDB rejects ADD COLUMN with a constraint.
ALTER TABLE engine_config ADD COLUMN mode TEXT;

-- One row per order the engine decided to place.
--
-- account_id rather than an engine_config id: config rows are deleted and
-- re-inserted on every reconfigure (EngineConfigRepo.upsert), and a foreign key
-- to them would refuse that delete. The account is the engine's stable identity.
--
-- A buy carries the entry PLAN (strategy, stop, target, ATR) the scan sized it
-- with, so 1b can open the position at the real fill price and re-derive the stop
-- from atr_at_entry via stop_distance exactly as plan_entry does — live R is then
-- measured from the price actually paid, not from the price the scan saw. A sell
-- carries the exit rule that wanted it and the position it closes.
--
-- ref_id is ours: a uuid the broker uses as its idempotency key, so an intent
-- re-sent after a crash cannot become two orders. approval_id exists because,
-- with trade approvals switched on, placing an order returns an APPROVAL (an id
-- that expires 16:00 ET the same day) and the order that eventually appears
-- carries the approval's id as its ref_id, not ours (observed 2026-10-21).
--
-- status: pending | submitted | awaiting_approval | filled | rejected |
-- cancelled | expired. The first three are open; an open buy holds a slot and
-- reserves its notional against settled cash. Validated in code (IntentStatus),
-- no CHECK, for the same reason as mode: a status 1b or 1c needs must not
-- require rebuilding a table.
CREATE SEQUENCE IF NOT EXISTS order_intent_id_seq;
CREATE TABLE IF NOT EXISTS order_intent (
    id BIGINT PRIMARY KEY DEFAULT nextval('order_intent_id_seq'),
    account_id BIGINT NOT NULL REFERENCES account (id),
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    side TEXT NOT NULL,                       -- buy | sell
    quantity DECIMAL(24, 8) NOT NULL,         -- 6dp fractional, as plan_entry sizes
    ref_price DECIMAL(24, 8) NOT NULL,        -- the price the scan saw
    notional DECIMAL(24, 8) NOT NULL,         -- quantity x ref_price
    reason TEXT NOT NULL,
    rule TEXT,                                -- exit rule key, sells only
    strategy TEXT NOT NULL,
    signal_id BIGINT REFERENCES engine_signal (id),      -- buys
    position_id BIGINT REFERENCES engine_position (id),  -- sells
    stop_price DECIMAL(24, 8),                -- buys: the entry plan
    target_price DECIMAL(24, 8),
    atr_at_entry DECIMAL(24, 8),
    ref_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    approval_id TEXT,
    broker_order_id TEXT,
    created_at TIMESTAMP NOT NULL,
    submitted_at TIMESTAMP,
    resolved_at TIMESTAMP,
    error TEXT,
    filled_quantity DECIMAL(24, 8),
    fill_price DECIMAL(24, 8),
    fees DECIMAL(24, 8)
);
