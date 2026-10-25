-- What the plan's uninvested cash earns while it waits (SB-1417). A staged plan
-- holds a large share in cash BY DESIGN, and at the broker that cash sits in a
-- core money-market position (the custodian's core fund), which pays a yield. Stating the
-- fund and its rate is what lets the report say the cash is working rather than
-- idle.
--
-- Its own table, not columns on alloc_plan: ALTER on a table other tables hold
-- foreign keys to is the migration that has already failed once (see 029). One
-- row per plan; no row = no yield stated. The rate is the person's reading of
-- the fund's 7-day yield on as_of — a money-market rate moves, so the date is
-- kept and a stale one is said out loud. Interest actually paid is income
-- (kind interest / cash_sweep), never derived from this.
CREATE TABLE IF NOT EXISTS alloc_cash_yield (
    plan_id BIGINT PRIMARY KEY REFERENCES alloc_plan (id),
    fund TEXT NOT NULL,
    yield_pct DECIMAL(9, 4) NOT NULL,
    as_of DATE NOT NULL
);
