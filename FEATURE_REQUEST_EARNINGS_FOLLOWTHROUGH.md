# Feature Request: Earnings Follow-Through Strategy

## Summary

Add an `earnings_followthrough` long-only strategy that seeks post-earnings continuation rather than guessing the binary pre-release move. The existing earnings blackout remains in place for ordinary strategies; this feature becomes eligible only after an earnings release is known and a confirmation window has passed.

This is a research feature. It must use point-in-time data and conservative execution assumptions before its performance is used for decisions.

## Problem

The current blackout appropriately avoids an unknown overnight gap, but it also means TRD cannot evaluate a common post-event pattern: a strong earnings result, positive forward guidance, estimate revisions, and sustained relative strength. The strategy needs structured earnings-event data, not merely an event date.

## Proposed behavior

### Eligibility

The strategy may evaluate only after the release timestamp:

- BMO: earliest entry is after the first 30-60 minutes of the same regular session.
- AMC: earliest entry is the next regular session after the opening stabilization window.
- Unknown release time: use the conservative next-session path.
- Never enter before the release, regardless of normal strategy signals.

### Initial long signal

Make all thresholds configurable; initial research defaults should be deliberately strict:

| Condition | Initial default |
| --- | --- |
| EPS surprise | >= +5% vs pre-release consensus |
| Revenue surprise | >= +3% vs pre-release consensus |
| Guidance | raised or reaffirmed; never lowered |
| Earnings reaction | positive regular-session return |
| Relative strength | positive vs SPY and SOXX/SMH |
| Confirmation | close above earnings-day midpoint and VWAP if available |
| Volume | >= 1.5x normal daily volume |
| Liquidity | minimum ADV and maximum spread thresholds |
| Gap protection | reject gaps above configurable chase threshold |

Analyst revisions should improve rank, not be a hard prerequisite initially:

- positive next-quarter and next-year estimate revision over the next 1-3 sessions increases score;
- negative revision vetoes the signal;
- unavailable revision data is explicitly reported and does not silently become a positive value.

### Entry and execution

- Use regular-session entries only in v1.
- Do not model after-hours fills until an after-hours market-data and execution model exists.
- Use the common execution model: spread, configurable slippage, commissions, and optional bar-volume participation cap.
- Record the actual decision timestamp, available-at timestamp for every feature, expected versus realized fill, and all scores.

### Exit behavior

Use event-aware exits in addition to the existing protections:

- Initial stop: below the earnings-day low or ATR stop, whichever is tighter.
- Invalidation: close below the earnings-day midpoint on strong volume.
- Take partial profit at configurable R multiple (initially 2R).
- Trail remainder using ATR or a 10-20 session low.
- Immediate exit/veto after a material negative guidance update or analyst revision.
- Time exit: test 5, 10, and 20 session holding windows independently.

## Data model

Add an `earnings_result` table. Preserve data as it was known at decision time; do not overwrite a historical consensus estimate with a later revision.

```text
instrument_id
released_at                 -- timezone-aware timestamp
release_timing              -- bmo | amc | during_market | unknown
source
source_observed_at
eps_actual
eps_estimate_pre_release
revenue_actual
revenue_estimate_pre_release
guidance_direction          -- raised | reaffirmed | lowered | none | unknown
next_quarter_revision_pct
next_year_revision_pct
earnings_day_return_pct
earnings_day_relative_strength_pct
quality_status
```

Update the existing earnings-event ingestion path to reconcile future dates:

- remove or supersede a rescheduled future event;
- retain historical releases immutably;
- track source and last-confirmed time;
- distinguish BMO/AMC/unknown timing.

## Semiconductor and AI extensions

For semiconductors and AI infrastructure, add optional structured features and ranking weights for:

- data-center/AI revenue and guidance;
- gross-margin direction;
- backlog, bookings, lead times, inventory, and supply commentary;
- HBM/memory pricing commentary;
- hyperscaler capex guidance;
- relative strength versus SOXX/SMH;
- peer earnings read-through.

Peer events should be recorded separately from a company’s own result. For example, hyperscaler capex guidance can affect semiconductor infrastructure names, but must not be treated as the target company’s earnings surprise.

## Backtest requirements

The feature must not ship with a backtest that has only date-level earnings data.

1. Every input has `available_at <= decision_at`.
2. Historical estimates are point-in-time snapshots.
3. Release timing controls the earliest eligible bar.
4. Opening-auction and post-gap fills include conservative slippage.
5. The backtest reports results by release timing, market regime, sector, gap bucket, holding window, and calendar year.
6. Walk-forward tuning uses training/validation/held-out windows, with frozen settings on the final holdout.
7. Results include gross and net return, turnover, max drawdown, gap loss, capacity/liquidity usage, and confidence intervals.

## Acceptance criteria

- [ ] Existing non-earnings strategies remain blocked before earnings.
- [ ] A BMO result cannot trigger an entry before the configured stabilization period; AMC/unknown results cannot trigger before the next session.
- [ ] A lowered-guidance result cannot produce a long signal, even with an EPS beat.
- [ ] Every stored signal includes the earnings-result ID and complete point-in-time feature snapshot.
- [ ] Earnings reschedules reconcile correctly without retaining a false future blackout.
- [ ] Backtest rejects event data missing a release timestamp or marks it ineligible rather than assuming a favorable time.
- [ ] Backtest and live-paper execution use the same entry/exit rule and execution-model code.
- [ ] Tests cover BMO, AMC, unknown timing, reschedule, guidance reversal, positive/negative revisions, large-gap rejection, and no-look-ahead.

## Non-goals for v1

- Trading the after-hours release itself.
- Options strategies or implied-volatility modeling.
- Automated LLM interpretation of calls/transcripts as a trade trigger.
- Short selling.

These can be subsequent research tracks once the point-in-time earnings data and regular-session execution model are trustworthy.

