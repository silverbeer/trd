"""Portable backup of the user-owned facts — the irreplaceable data that can't be
re-fetched from a provider: accounts, transactions, DCA plans, watchlists, and the
followed-indicator list (plus the instruments they reference). Prices, earnings, and
quotes are deliberately excluded — they rebuild with `trd sync`.

This is the durable cross-machine sync path: export on one Mac, restore on another,
then sync. Text/JSON (mergeable, no single-file-binary corruption risk), with IDs
remapped on restore so it loads cleanly into a fresh database."""

import json
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError

BACKUP_VERSION = 1


class BackupStats(BaseModel):
    instruments: int
    accounts: int
    transactions: int
    plans: int
    watchlists: int
    indicators: int


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
    return {
        "version": BACKUP_VERSION,
        "instruments": instruments,
        "accounts": accounts,
        "transactions": transactions,
        "plans": plans,
        "watchlists": watchlists,
        "indicators": indicators,
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
    if data.get("version") != BACKUP_VERSION:
        raise TrdError(
            f"Unsupported backup version {data.get('version')} (expected {BACKUP_VERSION})."
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

    from datetime import datetime

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

    return BackupStats(
        instruments=len(data["instruments"]),
        accounts=len(data["accounts"]),
        transactions=len(data["transactions"]),
        plans=len(data["plans"]),
        watchlists=len(data["watchlists"]),
        indicators=len(data["indicators"]),
    )
