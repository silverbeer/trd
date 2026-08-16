# TRD Code Review

## Executive summary

TRD is a strong local-first paper-trading research tool. Its shared live/backtest rule layer, explicit fill caveats, domain models, and broad test suite are good foundations. The main risks are simulation validity and operational consistency: some live and backtest behavior differs, intraday entry rules do not preserve their documented session-based meaning, and market-data/execution assumptions are still optimistic.

Static checks pass: Ruff, formatting, and ty.

## Top findings

1. **Critical: backtest permits same-bar re-entry after an exit.** `services/backtest.py` removes a closed symbol from `open_positions` before its entry pass, so it can immediately re-enter. The live scanner deliberately excludes `just_closed` symbols. This makes trade counts and results diverge.

2. **Critical: intraday entry lookbacks are bars, not sessions.** `engine/strategies.py` uses fixed 50/200/20/14 bar periods. On a 5-minute engine that makes the stated 200-day trend filter roughly 2.6 sessions. Exit periods already use `sessions_to_bars`; entry periods must do the same.

3. **Critical: execution assumptions are optimistic.** Backtests enter at signal close and exits use OHLC touch/quote prices, without spread, slippage, fees, partial fills, latency, or liquidity participation. Backtest results are research signals, not executable-return estimates.

4. **High: market-session safety lives in scheduler scripts, not the engine.** `EngineService.scan()` accepts naive local datetimes and does not enforce a US regular-session/holiday calendar. Direct CLI use or a new scheduler could act on extended-hours prices.

5. **High: scans are not atomic.** A scan writes runs, signals, transactions, and positions through separate statements. A failure between a transaction insert and position update can desynchronize the simulation ledger from engine state.

6. **High: market data is persisted without quality validation.** `PriceRepo` upserts each bar without checking OHLC validity, duplicate/gap coverage, outliers, split discontinuities, or partial provider responses.

7. **High: earnings dates lack release timing and reconciliation.** The store has a date but not BMO/AMC/timezone/source confidence. Upsert does not remove obsolete future dates when a company reschedules.

8. **Medium: historical tests retain survivorship bias.** Backtests use the current universe and current vendor history. Delisted names, historical index membership, corporate actions, and point-in-time universes are absent.

9. **Medium: CLI and rendering modules are too large.** `cli/app.py` and `cli/render.py` are respectively about 2,500 and 2,000 lines. They combine command registration, connection lifecycle, rendering, JSON behavior, and error translation.

10. **Medium: performance will not scale well beyond the small universe.** Price upserts issue one query per bar; scans load full histories and repeatedly recompute shared indicators for each strategy.

## Critical fixes

### Live/backtest parity

- In `simulate()`, collect symbols closed on the current stamp and exclude them from candidates, matching `EngineService.scan()`.
- Add a regression test proving an exit cannot be followed by a same-bar entry.
- Build parity fixtures that assert live and replayed decisions match for the same bars, timestamps, configuration, and execution model.

### Timeframe-aware entry strategies

- Pass timeframe or a strategy context into every entry strategy.
- Resolve all documented session lookbacks through `sessions_to_bars()`.
- Recalculate warmup requirements from the enabled strategy/timeframe.
- Add tests across `1d`, `5m`, `15m`, `30m`, and `1h`.

### Execution model

- Introduce an execution model shared by live-paper and backtest paths.
- Support configurable spread, slippage in basis points, commissions, and a maximum percentage of bar volume.
- Apply pessimistic defaults and render gross versus net performance.

## Architecture and Python recommendations

- Replace `exit_params: dict[str, float]` with typed Pydantic models such as `ExitRules`, `RegimeRules`, `DayModeRules`, and `ExecutionAssumptions`.
- Validate configuration on load as well as init: positive sizes, valid HHMM, non-negative budgets/blackouts, known strategies, and bounded parameters.
- Split CLI commands and renderers by domain; retain a thin root Typer app.
- Use a transaction boundary around each scan. On failure, roll back business writes and record a failed run with a reason in a separate recovery path.
- Use structured logging with run ID, symbol, strategy, bar timestamp, provider freshness, decision, and failure reason.
- Batch price upserts in a transaction.
- Cache indicators per symbol/bar/timeframe for one scan.

## Data and reliability recommendations

- Add bar validation: `low <= min(open, close) <= high`, non-negative volume, monotonic timestamps, duplicate detection, gaps, and anomaly thresholds.
- Store fetch provenance and coverage metadata with each provider batch.
- Make entry eligibility fail closed when data freshness/quality is unknown; positions should still be managed with the latest known price and marked stale.
- Move session validation into Python using timezone-aware America/New_York time and an exchange calendar; retain shell guards as defense in depth.
- Maintain a point-in-time universe and preserve vendor revisions where possible.

## Testing priorities

- Same-bar exit/re-entry parity.
- Entry session-scaling on all timeframes.
- Database failure injection at every scan write boundary.
- Provider malformed, missing, duplicate, stale, and anomalous bar tests.
- Half-day, holiday, DST, premarket, and after-hours tests.
- Splits/dividends and historical-data-restatement tests.
- Point-in-time earnings, BMO/AMC, reschedule, cancellation, and missing-date tests.
- Walk-forward tests with a held-out period and frozen parameters.

## Grade

| Area | Score |
| --- | ---: |
| Architecture | 7/10 |
| Code quality | 8/10 |
| Testability | 8/10 |
| Performance | 6/10 |
| Reliability | 6/10 |
| Trading logic | 5/10 |
| Production readiness | 5/10 |

TRD is well above average as a research/paper-trading system. Resolve the critical parity, timeframe, execution, and transaction issues before treating its scorecard as evidence for capital allocation.

