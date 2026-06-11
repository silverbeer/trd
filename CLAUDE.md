# trd — Investment Tracker

Local-first investment tracker. Full architecture and roadmap: [DESIGN.md](DESIGN.md).

## Commands

```bash
uv sync                     # install deps
uv run trd --help           # CLI entry point
uv run pytest               # tests (no network — FakeProvider in tests/conftest.py)
uv run ruff check . && uv run ruff format --check .
uv run ty check             # type checking
```

## CLI quick reference

```bash
trd init                              # create ~/.trd/trd.duckdb + 'main' account
trd account add fidelity              # one account per brokerage (--type simulation for paper)
trd account ls
trd sync [--full]                     # refresh quotes + daily bars + earnings (--full = 2y backfill)
trd portfolio [--account NAME]        # holdings with live P&L
trd lots [SYMBOL] [--account NAME]    # per-purchase detail: buy date, paid/share, total cost, gain
trd quote AAPL                        # live quote for any symbol
trd buy AAPL 10 [--price 213.50] [--account main] [--date 2026-06-10] [--fees 1] [--note ...]
trd sell AAPL 5 [--price ...]         # validates held quantity
trd import txns.csv                   # bulk-load transactions
trd watch add NVDA [--list ai]        # follow a symbol (creates list if needed)
trd watch rm NVDA [--list ai]
trd watch ls [ai]                     # quote board: price, day Δ%, 52w pos, vol/avg, next earnings
trd earnings [--days 14]              # upcoming earnings across everything tracked
```

CSV import format (header required): `date,account,symbol,side,quantity,price[,fees,note]` — date is ISO, side is buy/sell.

## Architecture rules (enforce in review)

- CLI layer ([src/trd/cli](src/trd/cli)) never touches DuckDB or yfinance directly — services only.
- Services ([src/trd/services](src/trd/services)) never import Typer/Rich — pure logic, fully testable.
- All market data goes through the `MarketDataProvider` protocol ([src/trd/providers/base.py](src/trd/providers/base.py)). Never import yfinance outside [src/trd/providers/yf.py](src/trd/providers/yf.py).
- Holdings are always derived from transactions via FIFO ([src/trd/services/fifo.py](src/trd/services/fifo.py)) — never stored as mutable balances.
- Schema changes = new numbered file in [src/trd/db/migrations](src/trd/db/migrations). Never edit an applied migration.
- Money/quantities are `Decimal` end to end. Never float.
- Tests never hit the network. Extend `FakeProvider` in [tests/conftest.py](tests/conftest.py).

## Environment

- DB lives at `~/.trd/trd.duckdb`; override root dir with `TRD_HOME` (tests do this).
- `trd` table for transactions is named `txn` (`transaction` is a reserved word).
