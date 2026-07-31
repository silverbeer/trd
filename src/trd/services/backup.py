"""Portable backup of the user-owned facts — the irreplaceable data that can't be
re-fetched from a provider: accounts, transactions, DCA plans, watchlists, the
followed-indicator list, exit triggers, and engine state (plus the instruments they
reference). Prices, earnings, and quotes are deliberately excluded — they rebuild
with `trd sync`.

Engine state travels with the transactions that produced it. A restore that brought
back the engine's buy/sell rows without `engine_position` would leave holdings the
engine no longer knows it owns — stops and targets silently gone.

This is the durable cross-machine sync path: export on one Mac, restore on another,
then sync. Text/JSON (mergeable, no single-file-binary corruption risk), with IDs
remapped on restore so it loads cleanly into a fresh database."""

import json
from datetime import date, datetime
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError
from trd.models import SizingMode
from trd.repos.engine import DEFAULT_EARNINGS_BLACKOUT_DAYS

BACKUP_VERSION = 3
# v1 predates exit triggers and the engine; v2 keyed engine signals by bar date and
# dropped the engine config fields added after it, so v3 reads either.
SUPPORTED_VERSIONS = (1, 2, 3)


class BackupStats(BaseModel):
    instruments: int
    accounts: int
    transactions: int
    plans: int
    watchlists: int
    indicators: int
    exit_triggers: int = 0
    engine_positions: int = 0
    engine_signals: int = 0


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _as_stamp(value: str) -> datetime:
    """A bar instant from either backup shape.

    v2 wrote a bare date, because a signal's bar was always a whole day. Midnight
    is that same instant, and is exactly what migration 015 converted the stored
    daily rows to — so an old backup restores onto the new key unchanged.
    """
    parsed = datetime.fromisoformat(value)
    return parsed


def _signal_stamp(signal: dict) -> datetime:
    value = signal.get("bar_ts") or signal["bar_date"]
    return _as_stamp(value)


def export_data(conn: duckdb.DuckDBPyConnection) -> dict:
    """Snapshot every user-owned table into a plain dict (JSON-ready)."""

    def rows(sql: str) -> list[tuple]:
        return conn.execute(sql).fetchall()

    instruments = [
        {
            "symbol": r[0],
            "name": r[1],
            "type": r[2],
            "exchange": r[3],
            "sector": r[4],
            "currency": r[5],
        }
        for r in rows("SELECT symbol, name, type, exchange, sector, currency FROM instrument")
    ]
    accounts = [
        {"name": r[0], "type": r[1], "currency": r[2]}
        for r in rows("SELECT name, type, currency FROM account")
    ]
    transactions = [
        {
            "account": r[0],
            "symbol": r[1],
            "side": r[2],
            "quantity": str(r[3]),
            "price": str(r[4]),
            "fees": str(r[5]),
            "executed_at": r[6].isoformat(),
            "note": r[7],
            # a txn's plan (if any) is the plan on its own account — restore re-links by account
            "has_plan": r[8] is not None,
        }
        for r in rows(
            """
            SELECT a.name, i.symbol, t.side, t.quantity, t.price, t.fees,
                   t.executed_at, t.note, t.plan_id
            FROM txn t JOIN account a ON a.id=t.account_id JOIN instrument i ON i.id=t.instrument_id
            ORDER BY t.executed_at, t.id
            """
        )
    ]
    plans = []
    for r in rows(
        """
        SELECT a.name, p.id, p.monthly_amount, p.strategy, p.strategy_ticker,
               p.note, p.day_of_month, p.active
        FROM contribution_plan p JOIN account a ON a.id=p.account_id
        """
    ):
        allocations = [
            {"symbol": s, "weight": str(w)}
            for s, w in rows(f"SELECT symbol, weight FROM plan_allocation WHERE plan_id={r[1]}")
        ]
        plans.append(
            {
                "account": r[0],
                "monthly_amount": str(r[2]),
                "strategy": r[3],
                "strategy_ticker": r[4],
                "note": r[5],
                "day_of_month": r[6],
                "active": r[7],
                "allocations": allocations,
            }
        )
    watchlists = []
    for wid, name in rows("SELECT id, name FROM watchlist"):
        symbols = [
            s
            for (s,) in rows(
                f"""SELECT i.symbol FROM watchlist_item wi
                    JOIN instrument i ON i.id=wi.instrument_id
                    WHERE wi.watchlist_id={wid}"""
            )
        ]
        watchlists.append({"name": name, "symbols": symbols})
    indicators = [
        {
            "key": r[0],
            "params": json.loads(r[1]) if isinstance(r[1], str) else r[1],
            "enabled": r[2],
            "display_order": r[3],
            "note": r[4],
        }
        for r in rows("SELECT key, params, enabled, display_order, note FROM indicator_config")
    ]
    exit_triggers = [
        {
            "account": r[0],
            "symbol": r[1],
            "stop_price": str(r[2]) if r[2] is not None else None,
            "target_price": str(r[3]) if r[3] is not None else None,
            "note": r[4],
        }
        for r in rows(
            """
            SELECT a.name, i.symbol, e.stop_price, e.target_price, e.note
            FROM exit_trigger e
            JOIN account a ON a.id=e.account_id
            JOIN instrument i ON i.id=e.instrument_id
            """
        )
    ]

    engine_config = None
    for r in rows(
        """
        SELECT a.name, c.watchlist, c.position_size, c.max_positions, c.strategies, c.exit_params,
               c.earnings_blackout_days, c.sizing_mode, c.timeframe
        FROM engine_config c JOIN account a ON a.id=c.account_id
        ORDER BY c.id DESC LIMIT 1
        """
    ):
        # Every field, not just the original six. A restore that silently drops
        # sizing_mode or timeframe hands back an engine that looks like the one
        # backed up and trades like a different one.
        engine_config = {
            "account": r[0],
            "watchlist": r[1],
            "position_size": str(r[2]),
            "max_positions": r[3],
            "strategies": json.loads(r[4]) if isinstance(r[4], str) else r[4],
            "exit_params": json.loads(r[5]) if isinstance(r[5], str) else r[5],
            "earnings_blackout_days": r[6],
            "sizing_mode": r[7],
            "timeframe": r[8],
        }

    # Signals are keyed by (symbol, strategy, bar_ts) — the same natural key the
    # scanner dedupes on — so positions can re-link to them without carrying IDs.
    engine_signals = [
        {
            "symbol": r[0],
            "strategy": r[1],
            "bar_ts": _iso(r[2]),
            "fired_at": _iso(r[3]),
            "price": str(r[4]),
            "score": r[5],
            "reason": r[6],
            "acted": r[7],
        }
        for r in rows(
            """
            SELECT i.symbol, s.strategy, s.bar_ts, s.fired_at, s.price, s.score, s.reason, s.acted
            FROM engine_signal s JOIN instrument i ON i.id=s.instrument_id
            ORDER BY s.fired_at, s.id
            """
        )
    ]
    engine_positions = [
        {
            "account": r[0],
            "symbol": r[1],
            "strategy": r[2],
            "opened_at": _iso(r[3]),
            "entry_price": str(r[4]),
            "quantity": str(r[5]),
            "stop_price": str(r[6]),
            "target_price": str(r[7]),
            "atr_at_entry": str(r[8]),
            "trail_high": str(r[9]),
            "bars_held": r[10],
            "last_bar_date": _iso(r[11]),
            "status": r[12],
            "closed_at": _iso(r[13]),
            "exit_price": str(r[14]) if r[14] is not None else None,
            "exit_reason": r[15],
            "signal_bar_ts": _iso(r[16]),
        }
        for r in rows(
            """
            SELECT a.name, i.symbol, p.strategy, p.opened_at, p.entry_price, p.quantity,
                   p.stop_price, p.target_price, p.atr_at_entry, p.trail_high, p.bars_held,
                   p.last_bar_date, p.status, p.closed_at, p.exit_price, p.exit_reason,
                   s.bar_ts
            FROM engine_position p
            JOIN account a ON a.id=p.account_id
            JOIN instrument i ON i.id=p.instrument_id
            LEFT JOIN engine_signal s ON s.id=p.signal_id
            ORDER BY p.opened_at, p.id
            """
        )
    ]

    return {
        "version": BACKUP_VERSION,
        "instruments": instruments,
        "accounts": accounts,
        "transactions": transactions,
        "plans": plans,
        "watchlists": watchlists,
        "indicators": indicators,
        "exit_triggers": exit_triggers,
        "engine": {
            "config": engine_config,
            "signals": engine_signals,
            "positions": engine_positions,
        },
    }


def _is_user_data_present(conn: duckdb.DuckDBPyConnection) -> bool:
    """True if the DB already holds restorable user data beyond a bare init."""
    txns = conn.execute("SELECT count(*) FROM txn").fetchone()
    accounts = conn.execute("SELECT count(*) FROM account WHERE name != 'main'").fetchone()
    return bool((txns and txns[0]) or (accounts and accounts[0]))


def restore_data(conn: duckdb.DuckDBPyConnection, data: dict) -> BackupStats:
    """Load a backup into a fresh database, remapping IDs. Refuses if the database
    already holds user data — restore rebuilds from scratch (the CLI's --force
    recreates the file first). Keeping this insert-only sidesteps a DuckDB catalog
    quirk where DELETE FROM a referenced parent table can fail after migrations."""
    if data.get("version") not in SUPPORTED_VERSIONS:
        raise TrdError(
            f"Unsupported backup version {data.get('version')} "
            f"(expected one of {', '.join(str(v) for v in SUPPORTED_VERSIONS)})."
        )
    if _is_user_data_present(conn):
        raise TrdError(
            "Database already has accounts/transactions. Restore rebuilds from scratch — "
            "pass --force to recreate the database from this backup."
        )

    # instruments — keep existing, add missing; build symbol -> id
    instrument_id: dict[str, int] = {
        r[0]: r[1] for r in conn.execute("SELECT symbol, id FROM instrument").fetchall()
    }
    for inst in data["instruments"]:
        if inst["symbol"] in instrument_id:
            continue
        row = conn.execute(
            """INSERT INTO instrument (symbol, name, type, exchange, sector, currency)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            [
                inst["symbol"],
                inst["name"],
                inst["type"],
                inst["exchange"],
                inst["sector"],
                inst["currency"],
            ],
        ).fetchone()
        assert row is not None
        instrument_id[inst["symbol"]] = row[0]

    account_id: dict[str, int] = {}
    for acc in data["accounts"]:
        row = conn.execute(
            "INSERT INTO account (name, type, currency) VALUES (?, ?, ?) RETURNING id",
            [acc["name"], acc["type"], acc["currency"]],
        ).fetchone()
        assert row is not None
        account_id[acc["name"]] = row[0]

    # plans first so transactions can re-link plan_id by account
    plan_id_for_account: dict[str, int] = {}
    for plan in data["plans"]:
        row = conn.execute(
            """INSERT INTO contribution_plan
                 (account_id, monthly_amount, strategy, strategy_ticker, note, day_of_month, active)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            [
                account_id[plan["account"]],
                Decimal(plan["monthly_amount"]),
                plan["strategy"],
                plan["strategy_ticker"],
                plan["note"],
                plan["day_of_month"],
                plan["active"],
            ],
        ).fetchone()
        assert row is not None
        plan_id_for_account[plan["account"]] = row[0]
        for alloc in plan["allocations"]:
            conn.execute(
                "INSERT INTO plan_allocation (plan_id, symbol, weight) VALUES (?, ?, ?)",
                [row[0], alloc["symbol"], Decimal(alloc["weight"])],
            )

    for txn in data["transactions"]:
        plan_id = plan_id_for_account.get(txn["account"]) if txn["has_plan"] else None
        conn.execute(
            """INSERT INTO txn (account_id, instrument_id, side, quantity, price, fees,
                                 executed_at, note, plan_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                account_id[txn["account"]],
                instrument_id[txn["symbol"]],
                txn["side"],
                Decimal(txn["quantity"]),
                Decimal(txn["price"]),
                Decimal(txn["fees"]),
                datetime.fromisoformat(txn["executed_at"]),
                txn["note"],
                plan_id,
            ],
        )

    for wl in data["watchlists"]:
        row = conn.execute(
            "INSERT INTO watchlist (name) VALUES (?) RETURNING id", [wl["name"]]
        ).fetchone()
        assert row is not None
        for symbol in wl["symbols"]:
            conn.execute(
                "INSERT INTO watchlist_item (watchlist_id, instrument_id) VALUES (?, ?)",
                [row[0], instrument_id[symbol]],
            )

    for ind in data["indicators"]:
        conn.execute(
            """INSERT INTO indicator_config (key, params, enabled, display_order, note)
               VALUES (?, ?, ?, ?, ?)""",
            [
                ind["key"],
                json.dumps(ind["params"]),
                ind["enabled"],
                ind["display_order"],
                ind["note"],
            ],
        )

    # v1 backups predate these sections; treat them as empty rather than failing.
    exit_triggers = data.get("exit_triggers", [])
    for trigger in exit_triggers:
        conn.execute(
            """INSERT INTO exit_trigger (account_id, instrument_id, stop_price, target_price, note)
               VALUES (?, ?, ?, ?, ?)""",
            [
                account_id[trigger["account"]],
                instrument_id[trigger["symbol"]],
                _dec(trigger["stop_price"]),
                _dec(trigger["target_price"]),
                trigger["note"],
            ],
        )

    engine = data.get("engine") or {}
    config = engine.get("config")
    if config is not None:
        conn.execute(
            """INSERT INTO engine_config
                 (account_id, watchlist, position_size, max_positions, strategies, exit_params,
                  earnings_blackout_days, sizing_mode, timeframe)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                account_id[config["account"]],
                config["watchlist"],
                Decimal(config["position_size"]),
                config["max_positions"],
                json.dumps(config["strategies"]),
                json.dumps(config["exit_params"]),
                # A v2 backup carries none of these. Their defaults are the
                # behaviour an engine written before each field already had.
                config.get("earnings_blackout_days", DEFAULT_EARNINGS_BLACKOUT_DAYS),
                config.get("sizing_mode") or SizingMode.EXPOSURE.value,
                config.get("timeframe") or "1d",
            ],
        )

    signals = engine.get("signals", [])
    signal_id: dict[tuple[str, str, str], int] = {}
    for signal in signals:
        stamp = _signal_stamp(signal)
        row = conn.execute(
            """INSERT INTO engine_signal
                 (instrument_id, strategy, bar_ts, fired_at, price, score, reason, acted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            [
                instrument_id[signal["symbol"]],
                signal["strategy"],
                stamp,
                datetime.fromisoformat(signal["fired_at"]),
                Decimal(signal["price"]),
                signal["score"],
                signal["reason"],
                signal["acted"],
            ],
        ).fetchone()
        assert row is not None
        signal_id[(signal["symbol"], signal["strategy"], stamp.isoformat())] = row[0]

    positions = engine.get("positions", [])
    for position in positions:
        stamp = position.get("signal_bar_ts") or position.get("signal_bar_date")
        key = (
            position["symbol"],
            position["strategy"],
            _as_stamp(stamp).isoformat() if stamp else "",
        )
        conn.execute(
            """INSERT INTO engine_position
                 (account_id, instrument_id, signal_id, strategy, opened_at, entry_price,
                  quantity, stop_price, target_price, atr_at_entry, trail_high, bars_held,
                  last_bar_date, status, closed_at, exit_price, exit_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                account_id[position["account"]],
                instrument_id[position["symbol"]],
                signal_id.get(key),
                position["strategy"],
                datetime.fromisoformat(position["opened_at"]),
                Decimal(position["entry_price"]),
                Decimal(position["quantity"]),
                Decimal(position["stop_price"]),
                Decimal(position["target_price"]),
                Decimal(position["atr_at_entry"]),
                Decimal(position["trail_high"]),
                position["bars_held"],
                date.fromisoformat(position["last_bar_date"])
                if position["last_bar_date"]
                else None,
                position["status"],
                datetime.fromisoformat(position["closed_at"]) if position["closed_at"] else None,
                _dec(position["exit_price"]),
                position["exit_reason"],
            ],
        )

    return BackupStats(
        instruments=len(data["instruments"]),
        accounts=len(data["accounts"]),
        transactions=len(data["transactions"]),
        plans=len(data["plans"]),
        watchlists=len(data["watchlists"]),
        indicators=len(data["indicators"]),
        exit_triggers=len(exit_triggers),
        engine_positions=len(positions),
        engine_signals=len(signals),
    )
