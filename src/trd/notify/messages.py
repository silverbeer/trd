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

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trd.engine import EXIT_REGISTRY
from trd.engine import REGISTRY as STRATEGIES
from trd.engine.bars import DAILY
from trd.models import StrategyStat
from trd.services.daily_report import DailyReport, EngineDay, ExitEvent
from trd.services.engine import ScanFill, ScanResult
from trd.services.review import EnginePack, ReviewResult


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


# ------------------------------------------------------------- daily report

# Longest a loss list is allowed to get. Past this the tail is a count: the
# reader wants the shape of a bad day, not every line of it.
MAX_LOSS_LINES = 5

# About as long as a daily message may get. The two engines that actually ship
# come in under it with every section; past it the optional ones give way, trade
# list first and losses summary second, because scrolling past detail to find out
# whether you are up is how a daily message stops being read. Sections 1, 2 and 4
# are never trimmed: they are bounded by the number of engines, not by the day.
MAX_LINES = 42

# Lines a loss list costs before any loss is in it: the blank, the heading, the
# worst-strategy line, and "today's losses by exit".
LOSS_OVERHEAD = 4

# Symbols named inside one exit rule's tally. Past this the reader has the shape
# of it and can get the rest from the trade list below.
MAX_NAMES_PER_RULE = 3

# Trades named per engine in TODAY'S TRADES. A day engine can close ten in a
# session; the best few and the worst few are the shape of the day, and the tail
# is a count. Sorted best-first, so the cut falls in the middle where it belongs.
MAX_TRADE_LINES = 6


def _rate(value: Decimal | None) -> str | None:
    return f"{float(value):.0f}% win" if value is not None else None


def _r(value: Decimal | None) -> str:
    return f"{float(value):+.2f}R" if value is not None else "—"


def _stat_line(stat: StrategyStat) -> str:
    """A strategy and what it actually did — named, because "the best strategy
    made +0.6R" is not something anyone can act on."""
    parts = [f"{_strategy_name(stat.strategy)} {_r(stat.expectancy_r)}", f"{stat.trades} trades"]
    rate = _rate(stat.win_rate)
    if rate:
        parts.append(rate)
    return " · ".join(parts)


def _pad(engines: list[EngineDay]) -> int:
    return max((len(e.engine) for e in engines), default=5)


def daily_report_message(report: DailyReport) -> str:
    """The post-market report as one message worth reading on a phone.

    Order is deliberate and is the order the questions get asked: am I up, what
    is working, what is not, and what is still exposed. Money is never a net on
    its own — realized and unrealized sit beside it, because an engine up only
    on open positions is a different engine from one up on closed ones.
    """
    engines = report.engines
    width = _pad(engines)
    both = len(engines) > 1

    def row(name: str, body: str) -> str:
        return f"{name.ljust(width)}  {body}"

    lines = [f"📊 trd daily — {report.on:%a %b %-d}"]

    # Before any number, not after: a stale mark makes every figure below it
    # wrong in a way that reads as a result.
    for engine in engines:
        if engine.error:
            lines.append(f"⚠ {engine.engine}: {engine.error}")
        elif engine.marks_are_stale:
            marked = f"{engine.marked_at:%b %-d}" if engine.marked_at else "an older session"
            lines.append(f"⚠ {engine.engine} marks are stale — priced at {marked}, not today")
    if not report.market_open:
        lines.append("⚠ no session stored for this date — the market may not have opened")

    live = [e for e in engines if e.error is None]
    if not live:
        return "\n".join(lines)

    lines += ["", "TODAY"]
    for engine in live:
        count = len(engine.exits_today)
        detail = f"{count} exit{'' if count == 1 else 's'}" if count else "no exits"
        lines.append(row(engine.engine, f"{_signed(engine.realized_today)} · {detail}"))
    if both:
        lines.append(
            row(
                "both",
                f"{_signed(report.realized_today)} · "
                f"{report.exits_today} exit{'' if report.exits_today == 1 else 's'}",
            )
        )

    lines += ["", "SINCE START"]
    for engine in live:
        lines.append(
            row(
                engine.engine,
                f"realized {_signed(engine.realized_all)} · "
                f"unrealized {_signed(engine.unrealized)} · NET {_signed(engine.net)}",
            )
        )
    if both:
        lines.append(
            row(
                "both",
                f"realized {_signed(report.realized_all)} · "
                f"unrealized {_signed(report.unrealized)} · NET {_signed(report.net)}",
            )
        )

    lines += ["", f"WORKING ({report.window_days}d)"]
    for engine in live:
        best = engine.best
        lines.append(
            row(engine.engine, _stat_line(best) if best else "not enough closed trades to say")
        )

    lines += ["", "OPEN BOOK (now)"]
    for engine in live:
        if engine.open_positions == 0:
            lines.append(row(engine.engine, "flat"))
            continue
        lines.append(
            row(
                engine.engine,
                f"{engine.open_positions} open · unrealized {_signed(engine.unrealized)} · "
                f"at risk {_money(engine.risk_at_stop)}",
            )
        )
    if both:
        lines.append(row("both", f"at risk {_money(report.risk_at_stop)}"))

    # Both optional sections are built last, sized by what is left of the screen,
    # and spliced back in above the open book so the reading order still runs
    # up/down -> working -> not working -> the trades -> exposure.
    #
    # The trade list gives way first and the losses summary second: knowing you
    # are down matters more than knowing which rule did it, which in turn matters
    # more than knowing which ticker. Sections 1, 2 and 4 are never cut.
    bad = _losses_section(report, live, row, budget=MAX_LINES - len(lines))
    trades = _trades_section(live, budget=MAX_LINES - len(lines) - len(bad))
    if bad or trades:
        insert = lines.index("OPEN BOOK (now)") - 1
        lines[insert:insert] = bad + trades
    return "\n".join(lines)


def _losses_section(
    report: DailyReport,
    live: list[EngineDay],
    row: Callable[[str, str], str],
    budget: int,
) -> list[str]:
    """The worst strategy over the window, and today's losing exits by rule.

    Each loss names its `exit_reason` rather than being summed with the rest: a
    day of stops is a broken thesis, a day of session_close is a day engine that
    never got paid, and one total cannot tell them apart.

    `budget` is how many lines are left on the screen. Too few for the heading
    and one loss and the section is dropped whole — a truncated "what went
    badly" is worse than none, because it looks like the full list.
    """
    if budget < LOSS_OVERHEAD + 1:
        return []
    lines: list[str] = ["", f"NOT WORKING ({report.window_days}d)"]
    named = False
    for engine in live:
        if engine.worst is None:
            continue
        lines.append(row(engine.engine, _stat_line(engine.worst)))
        named = True
    if not named:
        lines.append("no strategy has enough closed trades to blame yet")

    losses = [(e.engine, loss) for e in live for loss in e.losses_today]
    if not losses:
        return lines
    lines.append("today's losses by exit")
    groups: dict[str, list[ExitEvent]] = {}
    for _, loss in losses:
        groups.setdefault(_rule_name(loss.rule) or "closed", []).append(loss)
    # Worst group first, and one line per rule: five stops is a different day
    # from five bells, and that is the whole reason these are not summed. The
    # symbols are not repeated here — the trade list below names every one.
    ranked = sorted(groups.items(), key=lambda kv: sum(e.pnl for e in kv[1]))
    shown = max(1, min(MAX_LOSS_LINES, budget - len(lines) - 1))
    for rule, events in ranked[:shown]:
        total = sum((e.pnl for e in events), Decimal(0))
        lines.append(f"  {rule} x{len(events)} {_signed(total)}")
    if len(ranked) > shown:
        lines.append(f"  +{len(ranked) - shown} more exit rules")
    return lines


def _held(event: ExitEvent, timeframe: str) -> str | None:
    """How long the trade was on, in the unit the engine thinks in.

    Sessions on a swing engine, because that is what its rules count and what
    "held 10 sessions and only moved +6.5%" means. Elapsed time on an intraday
    one, where the same trade is 3 bars and 19 minutes, and only one of those
    tells a reader anything.
    """
    if timeframe != DAILY:
        return _duration(event.held_for)
    if event.bars_held <= 0:
        return None
    return f"{event.bars_held} session{'' if event.bars_held == 1 else 's'}"


def _trade_line(event: ExitEvent, timeframe: str) -> str:
    """One closed trade, whole: what it made, what it paid and sold for, how long
    it was on, and which rule ended it."""
    parts = [f"{event.symbol} {_signed(event.pnl)} ({_r(event.r_multiple)})"]
    if event.entry_price is not None and event.exit_price is not None:
        parts.append(f"{_money(event.entry_price)} → {_money(event.exit_price)}")
    held = _held(event, timeframe)
    if held:
        parts.append(held)
    parts.append(_rule_name(event.rule) or "closed")
    return "  " + " · ".join(parts)


def _trades_section(live: list[EngineDay], budget: int) -> list[str]:
    """Every exit today, best first, so the win of the day is the top line.

    The count in TODAY provokes exactly one question — which trades? — and until
    this existed the answer meant filtering `engine positions --all --json` by
    date. Capped per engine because a day engine closes ten in a session, and the
    cut falls in the middle of a best-first list, which is where a reader misses
    it least.
    """
    traded = [engine for engine in live if engine.exits_today]
    if not traded:
        return []
    # The blank line, the heading, and one label per engine when there is more
    # than one to tell apart.
    overhead = 2 + (len(traded) if len(live) > 1 else 0)
    room = budget - overhead
    # Not even one trade each: a list that names one engine's day and silently
    # omits the other's is worse than no list.
    if room < len(traded):
        return []
    allotment = min(MAX_TRADE_LINES, room // len(traded))

    lines: list[str] = ["", "TODAY'S TRADES"]
    for engine in traded:
        if len(live) > 1:
            lines.append(engine.engine)
        events = engine.exits_today
        if len(events) <= allotment:
            lines += [_trade_line(e, engine.timeframe) for e in events]
            continue
        # Both ends, not the top: the list is sorted best-first, so taking a
        # prefix would hide every loser behind "+5 more" — and "what went badly"
        # is half of what the report is for. The elision costs a line, so it
        # comes out of the allotment rather than on top of it.
        head = max(1, (allotment - 1) - (allotment - 1) // 2)
        tail = (allotment - 1) - head
        lines += [_trade_line(e, engine.timeframe) for e in events[:head]]
        lines.append(f"  +{len(events) - head - tail} more")
        if tail:
            lines += [_trade_line(e, engine.timeframe) for e in events[-tail:]]
    return lines


# ---------------------------------------------------------------- the review

# Telegram rejects a message over 4096 characters outright — the whole thing,
# not the tail. A review with six findings and their rationales can get there,
# so the message is built to a budget and says when it was cut.
REVIEW_MESSAGE_BUDGET = 3900
REVIEW_MAX_FINDINGS = 6
REVIEW_MAX_WATCH = 4


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def review_message(result: ReviewResult, packs: list[EnginePack], judged: Any | None = None) -> str:
    """The nightly decision review as one message worth reading on a phone.

    The arithmetic first and the model's read below it, visually separate, the
    same order the terminal prints them: what a detector computed and what a
    model concluded are different kinds of claim, and a reader has to be able
    to tell which is which at a glance. `judged` is the agent's `AiReviewRun`,
    typed loosely here on purpose — this module must import nothing from
    `trd.agents`, which lives behind an optional extra.

    "Nothing conclusive" is written out as a real answer. It is the expected one
    on most days, and a message that looked broken on a quiet day would train
    its reader to ignore the day it says something.
    """
    lines = [f"🔍 trd review — {result.on:%a %b %-d}"]
    width = max((len(p.engine) for p in packs), default=0)
    for pack in packs:
        day = pack.day
        lines.append(
            f"{pack.engine.ljust(width)}  {pack.config.timeframe} · {len(pack.trades)} closed, "
            f"{len(pack.signals)} signals · {_signed(day.realized_today)}"
        )
    for caveat in result.caveats:
        if "stale" in caveat or "no outcome measured" in caveat:
            lines.append(f"⚠ {_clip(caveat, 160)}")

    findings = result.findings
    if findings:
        hyp = result.hypotheses
        head = f"{len(findings)} finding{'' if len(findings) == 1 else 's'}"
        if hyp:
            head += f", {hyp} of them hypotheses"
        lines += ["", f"ARITHMETIC — {head}"]
        for finding in findings[:REVIEW_MAX_FINDINGS]:
            label = "HYPOTHESIS" if finding.hypothesis else "FINDING"
            lines.append(
                f"{label} {finding.engine}/{finding.subject} · {_clip(finding.headline, 120)} "
                f"(n={finding.n} over {finding.window_days}d)"
            )
            if finding.evidence:
                evidence = " · ".join(f"{k} {v}" for k, v in finding.evidence.items())
                lines.append(f"  {_clip(evidence, 140)}")
        if len(findings) > REVIEW_MAX_FINDINGS:
            lines.append(f"  … and {len(findings) - REVIEW_MAX_FINDINGS} more in trd engine review")
    else:
        lines += ["", "ARITHMETIC — nothing conclusive today"]

    if judged is not None:
        review = judged.review
        usage = judged.usage
        cost = getattr(usage, "cost_usd", None)
        tag = usage.model + (f" · ${float(cost):.2f}" if cost is not None else "")
        lines += ["", f"THE MODEL'S READ ({tag})", _clip(review.summary, 600)]
        if review.nothing_conclusive and not review.findings:
            lines.append("Nothing conclusive today.")
        for finding in review.findings[:REVIEW_MAX_FINDINGS]:
            label = "HYPOTHESIS" if finding.hypothesis else "FINDING"
            lines.append(
                f"{label} {_clip(finding.headline, 200)} — {finding.engine} · "
                f"{finding.scope}: {_clip(finding.subject, 40)} · n={finding.trades}"
            )
            if finding.rests_on:
                lines.append(f"  rests on: {_clip(' · '.join(finding.rests_on), 200)}")
            lines.append(f"  test: {_clip(finding.test, 160)}")
        if review.watch_next:
            lines += ["", "WATCH NEXT"]
            lines += [f" · {_clip(w, 200)}" for w in review.watch_next[:REVIEW_MAX_WATCH]]

    text = "\n".join(lines)
    if len(text) > REVIEW_MESSAGE_BUDGET:
        text = (
            text[: REVIEW_MESSAGE_BUDGET - 40].rstrip()
            + "\n… cut; the rest is in trd engine review"
        )
    return text
