from datetime import date, datetime
from decimal import Decimal

import duckdb

from trd.models.alloc import (
    AllocEvent,
    AllocTarget,
    CashFlow,
    CashFlowKind,
    Milestone,
    MilestoneStatus,
)

_FLOW_COLS = "id, account_id, kind, amount, occurred_at, note"


def _row_to_flow(row: tuple) -> CashFlow:
    return CashFlow(
        id=row[0],
        account_id=row[1],
        kind=CashFlowKind(row[2]),
        amount=row[3],
        occurred_at=row[4],
        note=row[5],
    )


class CashFlowRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def add(
        self,
        account_id: int,
        kind: CashFlowKind,
        amount: Decimal,
        occurred_at: datetime,
        note: str | None = None,
    ) -> CashFlow:
        row = self.conn.execute(
            f"""INSERT INTO cash_flow (account_id, kind, amount, occurred_at, note)
                VALUES (?, ?, ?, ?, ?) RETURNING {_FLOW_COLS}""",
            [account_id, kind.value, amount, occurred_at, note],
        ).fetchone()
        assert row is not None
        return _row_to_flow(row)

    def list_all(self, account_id: int | None = None) -> list[CashFlow]:
        """Oldest first — the order a cash-flow series wants."""
        where, params = ("WHERE account_id = ?", [account_id]) if account_id else ("", [])
        rows = self.conn.execute(
            f"SELECT {_FLOW_COLS} FROM cash_flow {where} ORDER BY occurred_at, id", params
        ).fetchall()
        return [_row_to_flow(r) for r in rows]


class AllocRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def plan_row(self, account_id: int) -> tuple | None:
        return self.conn.execute(
            """SELECT id, name, account_kind, brokerage, capital, drift_threshold,
                      created_at, note
               FROM alloc_plan WHERE account_id = ?""",
            [account_id],
        ).fetchone()

    def plan_accounts(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT account_id FROM alloc_plan").fetchall()]

    def create_plan(
        self,
        account_id: int,
        name: str,
        capital: Decimal,
        account_kind: str | None,
        brokerage: str | None,
        drift_threshold: Decimal,
        created_at: datetime,
        note: str | None,
    ) -> int:
        row = self.conn.execute(
            """INSERT INTO alloc_plan (account_id, name, account_kind, brokerage, capital,
                                       drift_threshold, created_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            [account_id, name, account_kind, brokerage, capital, drift_threshold, created_at, note],
        ).fetchone()
        assert row is not None
        return int(row[0])

    def update_plan(self, plan_id: int, **fields: object) -> None:
        allowed = {"name", "account_kind", "brokerage", "capital", "drift_threshold", "note"}
        sets = {k: v for k, v in fields.items() if v is not None}
        assert set(sets) <= allowed, set(sets) - allowed
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(
            f"UPDATE alloc_plan SET {assignments} WHERE id = ?", [*sets.values(), plan_id]
        )

    def targets(self, plan_id: int) -> list[AllocTarget]:
        return [
            AllocTarget(symbol=r[0], weight=r[1], label=r[2], pace_months=r[3], pace_start=r[4])
            for r in self.conn.execute(
                """SELECT symbol, weight, label, pace_months, pace_start
                   FROM alloc_target WHERE plan_id = ?
                   ORDER BY weight DESC, symbol""",
                [plan_id],
            ).fetchall()
        ]

    def replace_targets(self, plan_id: int, targets: list[AllocTarget]) -> None:
        self.conn.execute("DELETE FROM alloc_target WHERE plan_id = ?", [plan_id])
        for t in targets:
            self.conn.execute(
                """INSERT INTO alloc_target (plan_id, symbol, weight, label, pace_months,
                                            pace_start)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [plan_id, t.symbol, t.weight, t.label, t.pace_months, t.pace_start],
            )

    def milestones(self, plan_id: int) -> list[Milestone]:
        return [
            Milestone(
                id=r[0],
                seq=r[1],
                due_date=r[2],
                amount=r[3],
                status=MilestoneStatus(r[4]),
                note=r[5],
            )
            for r in self.conn.execute(
                """SELECT id, seq, due_date, amount, status, note FROM alloc_milestone
                   WHERE plan_id = ? ORDER BY seq""",
                [plan_id],
            ).fetchall()
        ]

    def replace_milestones(self, plan_id: int, rows: list[tuple[int, date, Decimal]]) -> None:
        self.conn.execute("DELETE FROM alloc_milestone WHERE plan_id = ?", [plan_id])
        for seq, due, amount in rows:
            self.conn.execute(
                "INSERT INTO alloc_milestone (plan_id, seq, due_date, amount) VALUES (?, ?, ?, ?)",
                [plan_id, seq, due, amount],
            )

    def update_milestone(self, milestone_id: int, **fields: object) -> None:
        allowed = {"due_date", "amount", "status", "note"}
        sets = {k: v for k, v in fields.items() if v is not None}
        assert set(sets) <= allowed, set(sets) - allowed
        assignments = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(
            f"UPDATE alloc_milestone SET {assignments} WHERE id = ?", [*sets.values(), milestone_id]
        )

    def log(self, plan_id: int, at: datetime, what: str) -> None:
        self.conn.execute(
            "INSERT INTO alloc_event (plan_id, logged_at, what) VALUES (?, ?, ?)",
            [plan_id, at, what],
        )

    def events(self, plan_id: int) -> list[AllocEvent]:
        return [
            AllocEvent(at=r[0], what=r[1])
            for r in self.conn.execute(
                "SELECT logged_at, what FROM alloc_event WHERE plan_id = ? ORDER BY logged_at, id",
                [plan_id],
            ).fetchall()
        ]
