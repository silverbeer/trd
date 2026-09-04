-- What a trade did while it was on, and what the signals we passed over did next.
--
-- The engine has always recorded the *result* of a trade — entry, exit, R. It
-- has never recorded the shape of it: how far underwater it went before it
-- worked, how much was on the table at its best, and what the price did after
-- the exit. Those are the three measurements behind "did we enter too early" and
-- "did we exit too early", and without them the questions can only be answered
-- by opinion.
--
-- Stored rather than recomputed on read, for two reasons. It is a walk over
-- every bar of every trade, which is not a thing to do inside a report; and the
-- review that reads these numbers has to see the same values twice running, or
-- its conclusions cannot be checked.
--
-- Everything is in R, never dollars: R is the unit the engine already reasons in
-- and the only one that makes a $10 day trade and a $100 swing comparable.
CREATE TABLE trade_outcome (
    position_id INTEGER PRIMARY KEY,
    computed_at TIMESTAMP NOT NULL,
    -- The bar width the walk ran on, and how many bars it saw. A trade measured
    -- on daily bars and one measured on 5-minute bars are not the same
    -- measurement, and a row that does not say which is not comparable.
    timeframe TEXT NOT NULL,
    bars_seen INTEGER NOT NULL,
    -- The denominator, carried so every ratio here can be re-derived by hand.
    risk_per_share DECIMAL(24, 8) NOT NULL,

    -- Maximum adverse excursion: the worst the trade looked, in R, and when.
    -- A winner that first went -0.9R was a bad entry that got lucky, and only
    -- this column can tell it from a good one.
    mae_r DECIMAL(18, 6),
    mae_at TIMESTAMP,
    mae_bar INTEGER,

    -- Maximum favourable excursion: the best it ever looked, and when. The bar
    -- index is half the point — a peak on bar 2 of a trade held 40 bars says
    -- the exit rule is slow, not that the entry was wrong.
    mfe_r DECIMAL(18, 6),
    mfe_at TIMESTAMP,
    mfe_bar INTEGER,

    -- What was actually booked, and what fraction of the best it kept. Capture
    -- is NULL when nothing was ever offered (MFE <= 0): you cannot give back
    -- what you never had, and a 0% there would read as an exit failure.
    exit_r DECIMAL(18, 6),
    capture DECIMAL(18, 6),

    -- Where price went after the exit, in R from the exit price. Positive means
    -- the trade kept working without us. `follow_through_seen` is how many bars
    -- were actually available: a trade that closed yesterday has no future yet,
    -- and zero bars must not read as zero movement.
    follow_through_r DECIMAL(18, 6),
    follow_through_bars INTEGER,
    follow_through_seen INTEGER
);

-- The same walk over signals the engine did NOT take.
--
-- `engine_signal` has recorded every signal, acted or not, since the engine
-- existed, and nothing has ever read the unacted ones. They are the only rows in
-- this system that can answer "are the rules filtering junk, or discarding
-- winners?" — and they are free, because they are already written.
--
-- The stop is the one the engine itself would have set: `plan_entry` is shared
-- with the live fill path, so the counterfactual R and the real R are the same
-- unit rather than two similar-looking ones.
CREATE TABLE signal_outcome (
    signal_id INTEGER PRIMARY KEY,
    computed_at TIMESTAMP NOT NULL,
    timeframe TEXT NOT NULL,
    acted BOOLEAN NOT NULL,
    -- Whether the book was full when this fired. Without it, "the passed signals
    -- would have made +40R" is fiction: most of them could not have been taken.
    capacity_blocked BOOLEAN NOT NULL,
    open_positions INTEGER,
    max_positions INTEGER,

    risk_per_share DECIMAL(24, 8),
    horizon_bars INTEGER NOT NULL,
    bars_seen INTEGER NOT NULL,

    mae_r DECIMAL(18, 6),
    mfe_r DECIMAL(18, 6),
    mfe_bar INTEGER,
    -- Where it stood at the end of the horizon, in R.
    forward_r DECIMAL(18, 6),
    -- Which came first inside the horizon: the 2R target or the 1R stop. This is
    -- the honest version of "what would it have done" — an average return says a
    -- signal was good on a path that ran -1.2R first, which the engine would
    -- have stopped out of and never seen.
    resolution TEXT
);
