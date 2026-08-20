-- Mark instruments that cannot be held.
--
-- ^VIX is an index: a number calculated from option prices, with no shares to
-- own. trd stores it because the market-regime gate reads it, and the gate never
-- trades it — but nothing enforced that. It has real OHLC bars (2026-08-20 ran
-- 14.91 to 16.08) and `pullback` and `macd_cross` read no volume, so a signal
-- could fire and the engine would open a paper position in it.
--
-- Why a column and not a new instrument.type: `type` carries
-- CHECK (type IN ('stock','etf','crypto')) from 001_init. DuckDB cannot drop or
-- widen a CHECK, and refuses to alter a column's type while one exists, so
-- adding 'index' would mean rebuilding `instrument` — a table 12 foreign keys
-- point at. Tradability is its own fact anyway, and this states it directly.
--
-- The backfill keys on the caret because that is yfinance's own convention for
-- an index (^VIX, ^GSPC, ^DJI), and a migration has no network to ask with.
-- Anything added later is marked at insert from the provider's quote type.
--
-- No NOT NULL: DuckDB refuses "Adding columns with constraints", so the DEFAULT
-- carries new rows and the UPDATE carries the existing ones.
ALTER TABLE instrument ADD COLUMN tradable BOOLEAN DEFAULT true;
UPDATE instrument SET tradable = true WHERE tradable IS NULL;
UPDATE instrument SET tradable = false WHERE symbol LIKE '^%';
