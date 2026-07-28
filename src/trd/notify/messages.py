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


def open_message(fill: ScanFill) -> str:
    return "\n".join(
        [
            f"🟢 BUY {fill.symbol} x{fill.quantity:g} @ {float(fill.price):,.2f}",
            f"strategy: {fill.strategy}",
            fill.reason,
        ]
    )


def close_message(fill: ScanFill) -> str:
    pnl = float(fill.pnl) if fill.pnl is not None else None
    r = float(fill.r_multiple) if fill.r_multiple is not None else None
    verdict = "🔴" if (pnl is not None and pnl < 0) else "🟦"
    result = f"{'+' if pnl is not None and pnl >= 0 else ''}{_money(pnl)}"
    if r is not None:
        result += f" ({r:+.2f}R)"
    return "\n".join(
        [
            f"{verdict} SELL {fill.symbol} x{fill.quantity:g} @ {float(fill.price):,.2f}",
            f"P&L: {result}",
            f"strategy: {fill.strategy} · exit rule: {fill.rule or '—'}",
            fill.reason,
        ]
    )


def scan_messages(result: ScanResult) -> list[str]:
    """Closes first — an exit that freed capital explains the entry that follows."""
    return [close_message(f) for f in result.closed] + [open_message(f) for f in result.opened]
