-- Intraday bars, so a day-mode engine has a series to reason over.
--
-- A day engine running on `price_daily` is inert by construction: a stop set at
-- 2 x the *daily* ATR cannot be reached inside one session, so every trade exits
-- on the clock and the R-multiples in `engine report` describe a risk profile the
-- engine never ran. Intraday bars are what make the stop and target reachable,
-- and what gives `engine backtest` a path to replay.
--
-- Kept separate from `price_daily` rather than folded in with a timeframe column:
-- daily history is a decade deep and permanent, intraday is ~60 days and rolls
-- off. Different retention, different provider limits, different sizes — one
-- table would make every daily query pay for the intraday rows.
--
-- No adj_close: over a 60-day window splits and dividends are the exception, and
-- the engine trades raw prices, not adjusted ones.
CREATE TABLE IF NOT EXISTS price_intraday (
    instrument_id BIGINT NOT NULL REFERENCES instrument (id),
    interval TEXT NOT NULL,      -- '5m', '15m' — the bar width, not a sample rate
    ts TIMESTAMP NOT NULL,       -- bar OPEN instant, exchange-local, as the provider stamps it
    open DECIMAL(24, 8),
    high DECIMAL(24, 8),
    low DECIMAL(24, 8),
    close DECIMAL(24, 8) NOT NULL,
    volume BIGINT,
    PRIMARY KEY (instrument_id, interval, ts)
);
