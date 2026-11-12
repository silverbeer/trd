-- Entries a scan ranked and could not pay for (SB-1628).
--
-- With a capital set, an entry settled cash cannot cover is refused, and the
-- only trace was a line in the scan's `skipped` — a pod log that dies with the
-- pod. A free slot with no money behind it then reads as "nothing to buy".
-- One row per refused entry per scan, so `engine status` and `live-report` can
-- say what was passed over, and the scan can alert once per session.
--
-- A log, not a fact the owner made: not in `trd backup`, like engine_run.
CREATE SEQUENCE IF NOT EXISTS engine_cash_refusal_id_seq;
CREATE TABLE IF NOT EXISTS engine_cash_refusal (
    id BIGINT PRIMARY KEY DEFAULT nextval('engine_cash_refusal_id_seq'),
    run_id BIGINT NOT NULL REFERENCES engine_run (id),
    account_id BIGINT NOT NULL REFERENCES account (id),
    refused_at TIMESTAMP NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    score DOUBLE NOT NULL,
    cost DECIMAL(24, 8) NOT NULL,
    buying_power DECIMAL(24, 8) NOT NULL,
    settles_on DATE,
    settles_amount DECIMAL(24, 8)
);
