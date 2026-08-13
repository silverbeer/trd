-- Exit lookbacks are denominated in sessions, not bars.
--
-- `max_bars` and `indicator_grace_bars` were counts of bars, named when every
-- engine ran on daily bars and the two were the same thing. The intraday
-- timeframe feature changed the bar width and left the thresholds alone, so on a
-- 5-minute engine a 10-bar time stop became 50 minutes and a 3-bar grace period
-- 15. Measured on the live day engine: 407 trades over 7 sessions, not one held
-- longer than 55 minutes, 84 of them closed by the time rule at exactly 50.
--
-- The *values* do not change — 10 and 3 always meant ten and three sessions, and
-- on a swing engine sessions and bars are the same unit, so every existing daily
-- engine keeps behaving exactly as it did. What changes is the name, and what it
-- resolves to on an intraday engine.
--
-- Renamed rather than reinterpreted in place: exit_params is validated against
-- DEFAULT_EXIT_PARAMS' key set, so a stale key is refused loudly at init rather
-- than read as a bar count by a build that thinks in sessions. A name that says
-- "bars" while meaning sessions is the defect being removed here, not something
-- to keep for compatibility.
--
-- exit_params is TEXT holding a JSON object. json_merge_patch follows RFC 7386,
-- where a null value *deletes* the key — that is how the old names are dropped.
-- COALESCE covers a config written before either key existed, falling back to the
-- documented defaults.
UPDATE engine_config
SET exit_params = json_merge_patch(
        json_merge_patch(exit_params, '{"max_bars": null, "indicator_grace_bars": null}'),
        json_object(
            'max_sessions',
            COALESCE(TRY_CAST(json_extract(exit_params, '$.max_bars') AS DOUBLE), 10.0),
            'indicator_grace_sessions',
            COALESCE(
                TRY_CAST(json_extract(exit_params, '$.indicator_grace_bars') AS DOUBLE), 3.0
            )
        )
    )::TEXT
WHERE json_extract(exit_params, '$.max_bars') IS NOT NULL
   OR json_extract(exit_params, '$.indicator_grace_bars') IS NOT NULL
   OR json_extract(exit_params, '$.max_sessions') IS NULL;
