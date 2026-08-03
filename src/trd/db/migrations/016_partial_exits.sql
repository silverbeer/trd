-- Partial exits: a position can be reduced without being closed.
--
-- Until now an engine position was all-or-nothing — one quantity, one exit price,
-- one closed_at — so "sell 90% at target and let the rest ride" had nowhere to
-- live, and taking cash out of a live position meant a manual sell that silently
-- desynced the two books (see the guard added in the previous change).
--
-- Two columns carry it:
--
--   closed_quantity  how much of the original size has been sold. The remainder
--                    is quantity - closed_quantity, and status flips to 'closed'
--                    only when that reaches zero.
--   booked_pnl       cash actually realised, accumulated across every partial.
--
-- Keeping `quantity` as the *original* size is what protects the R-multiple. R is
-- measured against the risk taken at entry, so the denominator must stay
-- risk_per_share x original quantity. Sell 90% at +2R and stop the remaining 10%
-- at -1R and the trade booked 0.9x2 + 0.1x(-1) = +1.7R — one number, still
-- meaningful, which is the whole reason the initial stop is immutable.
--
-- Added bare because DuckDB rejects ADD COLUMN with a constraint; the backfill
-- below gives every existing row the value it always implied.
ALTER TABLE engine_position ADD COLUMN closed_quantity DECIMAL(24, 8);
ALTER TABLE engine_position ADD COLUMN booked_pnl DECIMAL(24, 8);

-- A closed position sold all of it; an open one has sold none. Both were true
-- before this migration, they just had nowhere to be written down.
UPDATE engine_position
SET closed_quantity = CASE WHEN status = 'closed' THEN quantity ELSE 0 END,
    booked_pnl = CASE
        WHEN status = 'closed' AND exit_price IS NOT NULL
            THEN (exit_price - entry_price) * quantity
        ELSE 0
    END;
