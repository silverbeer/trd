-- What kind of company a symbol is, beyond Yahoo's eleven broad sectors.
--
-- `sector` alone puts NVDA and AAPL side by side as "Technology" and VRT under
-- "Industrials"; `industry` is the label that separates them. `summary` is the
-- provider's one-paragraph business description, so "what did I just add from
-- Telegram" has an answer without leaving trd.
--
-- All three are a provider's description and may be NULL — Yahoo has none for
-- most ETFs and indices, and returns a thin payload on a bad day. They are
-- refilled when a name is added again, never treated as a measurement.
ALTER TABLE instrument ADD COLUMN industry TEXT;
ALTER TABLE instrument ADD COLUMN country TEXT;
ALTER TABLE instrument ADD COLUMN summary TEXT;
