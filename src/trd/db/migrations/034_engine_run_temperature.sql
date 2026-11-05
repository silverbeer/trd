-- Market temperature per scan (SB-1538): S&P move, breadth over the engine's
-- universe, VIX, folded into one label. Stored as the JSON of the model so the
-- reading a report quotes is the reading the scan took, not one recomputed later
-- from bars that have since settled.
--
-- NULL on every run before this column, and on a pass with no quotes at all.
-- Added bare: DuckDB rejects ADD COLUMN with a constraint.
ALTER TABLE engine_run ADD COLUMN temperature VARCHAR;
