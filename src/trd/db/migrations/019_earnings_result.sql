-- Point-in-time earnings results.
--
-- `earnings_event` holds a *date* and whatever estimate the provider reports
-- right now, and it is rewritten on every sync. That is the correct shape for
-- the entry blackout, which only ever asks "is a report close?" — and it is
-- useless as evidence, because it cannot say what was known before the release.
-- A post-earnings strategy stands or falls on that distinction: an estimate
-- read after the fact is not a consensus, it is a memory of one.
--
-- This table is the archive. Rows are written once and never revised, so a
-- backtest can ask what the record looked like on the morning of a decision.
-- The reason it is worth building before the strategy that needs it: the
-- history cannot be bought back later. yfinance serves ~12 quarters and reports
-- only today's values, so every sync that runs without this table is a quarter
-- of point-in-time data permanently lost.
--
-- Most columns are unfillable from yfinance today (revenue, guidance, analyst
-- revisions, BMO/AMC timing). They exist anyway, NULL, because the alternative
-- is a schema change later that cannot retrofit the history it missed.
-- `quality_status` records which fields were actually observed, so a NULL is
-- never mistaken for a measurement, and never defaults to a favourable value.
CREATE TABLE IF NOT EXISTS earnings_result (
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    -- The session the result was released on. Date, not timestamp: yfinance does
    -- not publish a release time, and inventing one would be the exact kind of
    -- precision this table exists to avoid claiming.
    released_on DATE NOT NULL,
    -- bmo | amc | during_market | unknown. Always 'unknown' from yfinance, which
    -- is why entry eligibility has to take the conservative next-session path.
    release_timing TEXT NOT NULL DEFAULT 'unknown',
    source TEXT NOT NULL,
    -- When *this row* was observed. The half of point-in-time data that makes
    -- the other half meaningful: without it there is no way to assert
    -- available_at <= decision_at.
    source_observed_at TIMESTAMP NOT NULL,
    eps_actual DECIMAL(18, 6),
    -- The estimate as it stood when this row was first written. Never updated —
    -- overwriting it with a later revision is precisely the corruption the table
    -- is built to prevent.
    eps_estimate_pre_release DECIMAL(18, 6),
    revenue_actual DECIMAL(24, 4),
    revenue_estimate_pre_release DECIMAL(24, 4),
    -- raised | reaffirmed | lowered | none | unknown
    guidance_direction TEXT NOT NULL DEFAULT 'unknown',
    next_quarter_revision_pct DOUBLE,
    next_year_revision_pct DOUBLE,
    -- The reaction, computed once the session's daily bar has settled and then
    -- frozen. NULL until then.
    earnings_day_return_pct DOUBLE,
    earnings_day_relative_strength_pct DOUBLE,
    -- Comma-separated observation flags, e.g. 'eps,reaction' — which fields were
    -- actually measured rather than left unknown.
    quality_status TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (instrument_id, released_on)
);
