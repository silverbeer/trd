"""The timeframe vocabulary: what bar widths exist, and which pairings are legal.

Deliberately a leaf, and deliberately *outside* `trd.engine` — that package's
`__init__` pulls in the exit rules, which import `trd.models`, so anything a
model needs cannot live under it. Here a model can ask whether its own
configuration is sound without a circular import.
`trd.engine.bars` re-exports everything here, so importing either module works.
"""

from trd.errors import TrdError

DAILY = "1d"

# Bar width in minutes. The keys are the timeframes an engine may be configured
# with; anything else is refused at init rather than discovered at scan time.
INTRADAY_MINUTES: dict[str, int] = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}

TIMEFRAMES: tuple[str, ...] = (DAILY, *INTRADAY_MINUTES)

# How much intraday history a first sync asks for. The provider caps this anyway
# (yfinance serves ~60 days of 5-minute bars); asking for more is not an error,
# it just returns what exists.
INTRADAY_BACKFILL_DAYS = 59

# One regular session, 09:30-16:00. The unit every rule lookback is denominated
# in, so "20" means twenty sessions on every timeframe rather than twenty bars of
# whatever width the engine happens to run.
SESSION_MINUTES = 390

# Minute-of-day the regular session opens (09:30). With SESSION_MINUTES it gives
# the instant a *daily* bar stops moving, which is what tells a rule reading
# closes that today's close is finally a close. Deliberately not a market
# calendar: this engine has never had one, and half-days and holidays are handled
# by the shape of the data rather than by a table that has to be kept current.
SESSION_OPEN_MINUTE = 9 * 60 + 30


def bars_per_session(timeframe: str) -> int:
    """How many bars of this width make one session. Daily bars: one, by
    definition — which is what makes the scaling identity on a swing engine."""
    minutes = INTRADAY_MINUTES.get(validate_timeframe(timeframe))
    if minutes is None:
        return 1
    # Rounded, not floored: 1h bars are 6.5 to a session and the provider emits
    # seven (the last one short). Flooring would quietly shorten every 1h lookback.
    return max(1, (SESSION_MINUTES + minutes // 2) // minutes)


def sessions_to_bars(timeframe: str, sessions: float) -> int:
    """A lookback in sessions, resolved to bars of this timeframe.

    Rule periods used to be bar counts, which read the same on a swing engine and
    meant something else entirely on an intraday one: a "20-day" moving average
    became twenty 5-minute bars, or 100 minutes. The live day engine exited 228
    trades against that average and told each one it had "closed below the
    20-day". No trade it ever opened survived an hour.

    Sessions are the honest unit because they are the one the rules were tuned
    in. `bars_since` still counts bars — a trade's age is measured in what the
    engine can see — but the *threshold* it is compared against is a duration.

    Not floored at one bar: zero sessions has to stay zero. `max_sessions: 0`
    means "close it on the next bar", which is how the entry/exit interaction
    tests force a same-bar close, and rounding that up to one bar silently
    disables them.
    """
    return max(0, round(sessions * bars_per_session(timeframe)))


def day_mode_on_daily_bars(timeframe: str, flat_at_minute: int) -> str | None:
    """The diagnosis for the one configuration an engine must never run in.

    Returns the reason, or None when the pairing is sound. Shared so that `init`
    (which refuses it) and `status` (which has to report an engine already living
    in it) cannot drift into describing the same defect two different ways — the
    guard used to exist only at init, so an engine created before it kept running
    unguarded and nothing said so.

    Callers append their own remedy: init tells you which flag to pass, status
    tells you the engine needs rebuilding.
    """
    if timeframe == DAILY and flat_at_minute > 0:
        return (
            "A day engine (--flat-at) needs an intraday timeframe. On daily bars "
            "its stop and target cannot be reached inside one session, so every "
            "trade exits on the clock."
        )
    return None


def validate_timeframe(timeframe: str) -> str:
    if timeframe not in TIMEFRAMES:
        raise TrdError(
            f"Unknown timeframe {timeframe!r}. Available: {', '.join(TIMEFRAMES)}. "
            f"'{DAILY}' is a swing engine; the rest read intraday bars."
        )
    return timeframe
