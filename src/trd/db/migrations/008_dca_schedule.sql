-- DCA flagship phase 1: plans gain a schedule (day of month) and lifecycle
-- state; price history gains adjusted close so multi-year return math includes
-- dividends and splits. Analytics read COALESCE(adj_close, close); the txn
-- ledger keeps real executed prices.
-- DuckDB ALTER ADD COLUMN cannot carry NOT NULL; default + backfill instead.
ALTER TABLE contribution_plan ADD COLUMN IF NOT EXISTS day_of_month INTEGER;
ALTER TABLE contribution_plan ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE;
UPDATE contribution_plan SET active = true WHERE active IS NULL;
ALTER TABLE price_daily ADD COLUMN IF NOT EXISTS adj_close DECIMAL(18, 6);
