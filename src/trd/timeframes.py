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
