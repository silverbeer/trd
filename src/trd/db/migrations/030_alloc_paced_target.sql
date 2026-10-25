-- A PACED target (SB-1388): its weight is earmarked from the plan's capital and
-- bought in equal monthly instalments over pace_months, starting pace_start —
-- an IRA's single-name moonshot, 3% bought over 24 months. NULL = an
-- ordinary target, filled by the tranches like any other fund.
--
-- The unbought part of the earmark is cash held for that purpose: shown apart
-- from free cash, never counted twice, and left out of the tranches' fund share
-- so a milestone is not "partial" for want of money that was never meant for it.
ALTER TABLE alloc_target ADD COLUMN pace_months INTEGER;
ALTER TABLE alloc_target ADD COLUMN pace_start DATE;
