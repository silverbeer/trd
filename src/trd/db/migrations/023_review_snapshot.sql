-- Dated decision reviews, one row per session per engine.
--
-- Same shape and the same reason as `prep_snapshot` (009): a few denormalized
-- columns so "how has this drifted" is a query, and the whole review as JSON for
-- everything else.
--
-- The point is not the review. The point is that a claim made on Tuesday can be
-- checked on Friday against what actually happened — which makes the reviews
-- themselves reviewable, and the reviewer's own hit rate on its suggestions the
-- number that decides whether any of this is worth running.
--
-- Per engine, because one database is one engine. A combined view is assembled
-- by the command that reads both; storing half a review in each database would
-- make either one alone a lie.
CREATE SEQUENCE IF NOT EXISTS review_snapshot_id_seq;
CREATE TABLE IF NOT EXISTS review_snapshot (
    id BIGINT PRIMARY KEY DEFAULT nextval('review_snapshot_id_seq'),
    snapshot_date DATE NOT NULL UNIQUE,   -- the session reviewed
    generated_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    build TEXT NOT NULL,                  -- which code drew the conclusions
    engine TEXT NOT NULL,
    trades_closed INTEGER NOT NULL DEFAULT 0,
    signals_fired INTEGER NOT NULL DEFAULT 0,
    findings INTEGER NOT NULL DEFAULT 0,
    hypotheses INTEGER NOT NULL DEFAULT 0,
    -- Whether the honest answer was "nothing conclusive". Its own column because
    -- the share of quiet days is the first health check on a reviewer: one that
    -- finds something every day is fitting noise, and that has to be countable
    -- without parsing the payload.
    quiet BOOLEAN NOT NULL DEFAULT TRUE,
    payload JSON NOT NULL
);
