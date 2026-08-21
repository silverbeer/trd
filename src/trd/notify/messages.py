"""Turning a scan into the handful of lines worth interrupting someone for.

Scans are quiet the overwhelming majority of the time, so notifying on every pass
would train you to ignore the channel. Only fills are pushed: a position opening
or closing is the engine actually doing something. Signals it declined stay in the
log and the dashboard.

A closed trade gets sections — trade, risk, thesis, execution — because the
questions a human asks afterwards are always the same four: what happened, what
did it cost, why was it on, and did it fill where it should have. Every figure
comes off `ScanFill`, which carries what the position already knew; nothing here
recomputes a stop or a risk number.

Sections whose data is absent are dropped rather than printed with dashes. An
indicator exit has no trigger price, so it has no execution section, and a message
padded with "—" reads as broken rather than as inapplicable.

Plain text, no markup — see TelegramNotifier for why.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from trd.engine import EXIT_REGISTRY
from trd.engine import REGISTRY as STRATEGIES
from trd.services.engine import ScanFill, ScanResult


def _money(value: float | Decimal | None) -> str:
    return f"{float(value):,.2f}" if value is not None else "—"


def _signed(value: float | Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{'+' if float(value) >= 0 else '-'}{abs(float(value)):,.2f}"


def _stamp(moment: datetime | None) -> str | None:
    """ "Aug 18, 09:45" — the date a swing trade needs and the time an intraday one
    does. Times are the engine's own local clock, the same one every other trd
    surface prints."""
    return f"{moment:%b %-d, %H:%M}" if moment else None


def _clock(moment: datetime | None) -> str | None:
    return f"{moment:%H:%M}" if moment else None


def _leg(price: Decimal, moment: datetime | None, same_session: bool) -> str:
    """A price and when it happened. The exit drops the date when the trade opened
    and closed on the same session — repeating it is noise on a day trade, and its
    absence is what makes a multi-day hold visible at a glance."""
    when = _clock(moment) if same_session else _stamp(moment)
    return f"{_money(price)} · {when}" if when else _money(price)


def _duration(span: timedelta | None) -> str | None:
    """Whole units, largest two: "2h 14m", "3d 4h", "45m". A trade held eleven
    seconds and one held eleven minutes are different trades, and one held three
    days does not need its minutes."""
    if span is None:
        return None
    total = int(span.total_seconds())
    if total < 60:
        return f"{total}s"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _strategy_name(key: str) -> str:
    """The rule's own display name. The registry already knows it is "MACD Cross";
    a notification that says "macd_cross" is leaking a dict key at a human."""
    rule = STRATEGIES.get(key)
    return rule.name if rule else key


def _rule_name(key: str | None) -> str | None:
    if key is None:
        return None
    rule = EXIT_REGISTRY.get(key)
    return rule.name if rule else key


def _tag(label: str | None) -> str:
    """Which engine is talking. Two engines commonly share one chat — a swing one
    that carries positions overnight and a day one that is flat by the bell — and
    the same symbol can sit in both universes, so an unlabelled fill is ambiguous
    about the one thing that decides what to do with it."""
    return f"[{label}] " if label else ""


def _section(title: str, rows: list[tuple[str, str | None]]) -> list[str]:
    """A titled block, or nothing at all when every row is missing."""
    present = [(k, v) for k, v in rows if v is not None]
    if not present:
        return []
    return ["", title, *[f"{k}: {v}" for k, v in present]]


def open_message(fill: ScanFill, label: str | None = None) -> str:
    lines = [
        f"{_tag(label)}🟢 BUY {fill.symbol} — {_strategy_name(fill.strategy)}",
        f"{fill.quantity:g} sh @ {_money(fill.price)}",
    ]
    # The question a buy alert has to answer. Labelled, not left as a bare line
    # under the price, because "why is this on" is what a reader is looking for.
    lines += _section("Why we bought", [("Trigger", fill.reason)])
    # What this trade risks, stated before it risks it.
    lines += _section(
        "Risk",
        [
            ("Stop", _money(fill.stop_price) if fill.stop_price is not None else None),
            ("Target", _money(fill.target_price) if fill.target_price is not None else None),
            (
                "Risk",
                f"{_money(fill.risk_per_share)}/share" if fill.risk_per_share is not None else None,
            ),
            ("1R", _money(fill.planned_1r) if fill.planned_1r is not None else None),
        ],
    )
    return "\n".join(lines)


def _headline(fill: ScanFill, label: str | None) -> str:
    """Name the outcome, not just the side. "STOPPED OUT" is the thing a reader
    is actually scanning for; SELL is true of every exit and says nothing."""
    pnl = float(fill.pnl) if fill.pnl is not None else None
    verdict = "🔴" if (pnl is not None and pnl < 0) else "🟦"
    outcome = {
        "stop": "STOPPED OUT",
        "trail": "TRAILED OUT",
        "target": "TARGET HIT",
        "time": "TIME EXIT",
        "indicator": "THESIS BROKEN",
        "session_close": "FLAT AT THE BELL",
    }.get(fill.rule or "", "CLOSED")
    return f"{_tag(label)}{verdict} {fill.symbol} — {outcome}"


def close_message(fill: ScanFill, label: str | None = None) -> str:
    pnl = float(fill.pnl) if fill.pnl is not None else None
    r = float(fill.r_multiple) if fill.r_multiple is not None else None
    # None, not "—", when there is no P&L to state: `_section` drops a missing row
    # entirely, and a dash-padded line reads as broken rather than inapplicable.
    result = None
    if pnl is not None:
        result = _signed(pnl)
        if r is not None:
            # Realized R, next to the planned 1R above it. The two are deliberately
            # adjacent and deliberately labelled differently: one is what was put
            # at risk, the other what came back in those units.
            result += f" ({r:+.2f}R)"

    same_session = (
        fill.opened_at is not None
        and fill.closed_at is not None
        and fill.opened_at.date() == fill.closed_at.date()
    )
    lines = [_headline(fill, label)]
    lines += _section(
        "Trade",
        [
            (
                "Entry",
                _leg(fill.entry_price, fill.opened_at, False)
                if fill.entry_price is not None
                else None,
            ),
            ("Exit", _leg(fill.price, fill.closed_at, same_session)),
            ("Size", f"{fill.quantity:g} sh ({_money(fill.price * fill.quantity)})"),
            ("Held", _duration(fill.held_for)),
        ],
    )
    lines += _section(
        "Risk",
        [
            ("Stop", _money(fill.stop_price) if fill.stop_price is not None else None),
            (
                "Risk",
                f"{_money(fill.risk_per_share)}/share" if fill.risk_per_share is not None else None,
            ),
            ("1R", _money(fill.planned_1r) if fill.planned_1r is not None else None),
            ("Target", _money(fill.target_price) if fill.target_price is not None else None),
            ("P&L", result),
        ],
    )
    # Two questions, asked separately because they have different answers and a
    # reader wants one or the other: what put this trade on, and what took it off.
    lines += _section(
        "Why we bought",
        [
            ("Strategy", _strategy_name(fill.strategy)),
            ("Trigger", fill.setup),
        ],
    )
    lines += _section(
        "Why we exited",
        [
            ("Rule", _rule_name(fill.rule)),
            ("Trigger", fill.reason),
        ],
    )
    # Only when a rule named a level. An indicator or time exit has no intended
    # price, so there is nothing for the fill to have slipped against.
    slip = fill.slippage_per_share
    lines += _section(
        "Execution",
        [
            # "Level", not "Triggered": the exit section above already uses
            # "Trigger" for the rule's own words, and the same word meaning a
            # sentence there and a price here is how a reader misreads both.
            ("Level", _money(fill.trigger_price) if fill.trigger_price is not None else None),
            ("Fill", _money(fill.price) if fill.trigger_price is not None else None),
            (
                "Slippage",
                f"{_signed(slip)}/share ({_signed(fill.slippage_total)})"
                if slip is not None
                else None,
            ),
        ],
    )
    return "\n".join(lines)


def scan_messages(result: ScanResult, label: str | None = None) -> list[str]:
    """Closes first — an exit that freed capital explains the entry that follows."""
    return [close_message(f, label) for f in result.closed] + [
        open_message(f, label) for f in result.opened
    ]
