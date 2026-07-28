-- Entry blackout around earnings.
--
-- The engine's scorecard is denominated in R, and every claim it makes rests on
-- the stop bounding the loss at 1R. An overnight earnings gap jumps straight
-- through a 2 x ATR stop: StopLoss compares price to the stop level, so the exit
-- fills at whatever exists the next morning, not at the stop. A trade the engine
-- believes risks 1R can realise several. Those trades are not comparable to the
-- rest, so averaging them into `trd engine report` makes it describe a risk
-- profile the engine is not running.
--
-- This is an *entry-side* parameter, so it does not belong in exit_params — that
-- JSON is named for the exit rules and validated against their key set.
--
-- 0 disables the blackout, which is what a backtest of the pre-blackout
-- behaviour would want.
-- Added bare, then backfilled: DuckDB rejects ALTER TABLE ... ADD COLUMN with a
-- constraint ("Adding columns with constraints not yet supported").
ALTER TABLE engine_config ADD COLUMN earnings_blackout_days INTEGER;
UPDATE engine_config SET earnings_blackout_days = 3 WHERE earnings_blackout_days IS NULL;
