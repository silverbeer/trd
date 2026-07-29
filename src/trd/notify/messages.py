"""Turning a scan into the handful of lines worth interrupting someone for.

Scans are quiet the overwhelming majority of the time, so notifying on every pass
would train you to ignore the channel. Only fills are pushed: a position opening
or closing is the engine actually doing something. Signals it declined stay in the
log and the dashboard.

Plain text, no markup — see TelegramNotifier for why.
"""

from trd.services.engine import ScanFill, ScanResult


def _money(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None else "—"


def _tag(label: str | None) -> str:
    """Which engine is talking. Two engines commonly share one chat — a swing one
    that carries positions overnight and a day one that is flat by the bell — and
    the same symbol can sit in both universes, so an unlabelled fill is ambiguous
    about the one thing that decides what to do with it."""
    return f"[{label}] " if label else ""


def open_message(fill: ScanFill, label: str | None = None) -> str:
    return "\n".join(
        [
            f"{_tag(label)}🟢 BUY {fill.symbol} x{fill.quantity:g} @ {float(fill.price):,.2f}",
            f"strategy: {fill.strategy}",
            fill.reason,
        ]
    )


def close_message(fill: ScanFill, label: str | None = None) -> str:
    pnl = float(fill.pnl) if fill.pnl is not None else None
    r = float(fill.r_multiple) if fill.r_multiple is not None else None
    verdict = "🔴" if (pnl is not None and pnl < 0) else "🟦"
    result = f"{'+' if pnl is not None and pnl >= 0 else ''}{_money(pnl)}"
    if r is not None:
        result += f" ({r:+.2f}R)"
    return "\n".join(
        [
            f"{_tag(label)}{verdict} SELL {fill.symbol} x{fill.quantity:g} "
            f"@ {float(fill.price):,.2f}",
            f"P&L: {result}",
            f"strategy: {fill.strategy} · exit rule: {fill.rule or '—'}",
            fill.reason,
        ]
    )


def scan_messages(result: ScanResult, label: str | None = None) -> list[str]:
    """Closes first — an exit that freed capital explains the entry that follows."""
    return [close_message(f, label) for f in result.closed] + [
        open_message(f, label) for f in result.opened
    ]
