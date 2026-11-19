-- Human slots on a live engine (SB-1742): the owner picks the ENTRY, the engine
-- manages the EXIT with its own rules. A human slot is an ordinary
-- engine_position whose strategy is 'human', opened from a buy intent the owner
-- asked for ('trd engine human buy') rather than one a scan ranked.
--
-- One capital, two sleeves. engine_config.capital is the account's total; this
-- row says how much of it is the human sleeve, how many slots it has and its own
-- bridge. The engine sleeve is capital - human capital, so adding human slots
-- never shrinks what the engine may spend. Each sleeve replays its own fills
-- into its own T+1 ledger, so a human buy cannot spend the engine's cash and a
-- cash-blocked entry stays attributable to one side.
--
-- The human sleeve has its own loss stop (max_loss, halted*). It halts human
-- BUYS only; exits keep running. The engine's guard (028) is engine-only from
-- this migration on: a human loss never halts the engine, and the engine's halt
-- never withholds a human order.
--
-- Its own table, keyed by account like engine_guard and for the same reason:
-- it survives 'engine cash --set', a re-init and backup/restore.
CREATE TABLE IF NOT EXISTS engine_human (
    account_id BIGINT PRIMARY KEY REFERENCES account (id),
    slots INTEGER NOT NULL,
    capital DECIMAL(18, 6) NOT NULL,      -- dollars of the total capital
    bridge DECIMAL(18, 6),                -- NULL = no bridge (see 033)
    max_loss DECIMAL(18, 6),              -- dollars, human realized + unrealized floor
    halted BOOLEAN NOT NULL DEFAULT FALSE,
    halt_reason TEXT,
    halted_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- "Let it run": a human position the owner has told the engine not to take
-- profit on. Its profit target and time exit are skipped; the stop, the trail and
-- the indicator exit still apply, so a winner rides the trail and a loser still
-- stops out. A row is the flag; deleting it clears the flag.
--
-- No foreign key to engine_position, deliberately: a referenced row is one DuckDB
-- refuses some UPDATEs on (see 036), and engine_position is updated every scan.
CREATE TABLE IF NOT EXISTS engine_position_let_run (
    position_id BIGINT PRIMARY KEY,
    set_at TIMESTAMP NOT NULL
);
