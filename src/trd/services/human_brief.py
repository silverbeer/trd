"""The human-slot brief (SB-1754): a post-close read for the owner's own picks.

A human slot (SB-1742) is a position whose ENTRY the owner chooses and whose
exit the engine runs. Nothing helped choose that entry. This is the read that
does, in three blocks:

1. **Market state** — six signals that tend to appear before a sell-off turns,
   each on, off or unknown with the numbers behind it. Facts about the last
   few sessions, never a forecast.
2. **Candidates** — names in the engine's universe that are stretched below
   their long average AND have turned, with the stop and target `plan_entry`
   would set. A screen, not a recommendation, and never an order: the owner
   still runs `trd engine human buy SYM`.
3. **Flags** — earnings inside the expected hold, a name that already has an
   owner, a sleeve with no room.

The candidate rule is UNPROVEN until measured, which is what `study` is for:
every fresh firing of the rule in stored history set beside random names on
the same session, each priced with the stop `stop_distance` would have set
and walked by the outcome study's own `excursion` and `resolution`.

Everything here reads daily bars, so every lookback is already in sessions —
there is no bar-count conversion to get wrong. Read-only: nothing is written
and no provider is called.
"""

import math
import random
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from pydantic import BaseModel, computed_field

from trd.data.market_calendar import MARKET_HOLIDAYS
from trd.engine.bars import DAILY, BarSource
from trd.engine.base import indicator
from trd.models import DailyBar, Instrument, Side, SizingMode, is_human
from trd.services.engine import EngineService, entry_levels, plan_entry, stop_distance
from trd.services.outcomes import excursion, resolution

# --- thresholds. Module constants on purpose: a screen with a tuned threshold
# --- is a model with extra steps, so each is stated once and measured as is.

# Sessions of history a signal looks back over.
LOOKBACK = 20
# A low has to be this many sessions old before a later low can be "higher".
HIGHER_LOW_MIN_SESSIONS = 3
# A new low counts as fresh for this many sessions.
FRESH_LOW_SESSIONS = 5
# Capitulation bar: opens this far below the prior close, closes green, on volume.
GAP_DOWN_PCT = 1.0
HEAVY_VOLUME_MULTIPLE = 1.5
CAPITULATION_WINDOW = 3
# Breadth = share of names closing up. Washout then thrust, inside this window.
BREADTH_WINDOW = 10
WASHOUT_PCT = 15.0
THRUST_PCT = 80.0
MIN_BREADTH_NAMES = 20
# Leadership: the worst performers of the last N sessions against the best.
LEADERSHIP_SESSIONS = 10
# RSI divergence: the recent low against the low of the window before it.
RSI_PERIOD = 14
RSI_RECENT = 5
RSI_PRIOR = 30

# Candidate rule. Setup: this far below the long average. Trigger: a reclaim
# of the short average inside RECLAIM_WINDOW sessions, or a higher low.
LONG_SMA = 200
TURN_SMA = 20
STRETCH_MIN_PCT = 10.0
RECLAIM_WINDOW = 3
# "4-year average": 4 x 252 sessions. A column, not part of the rule.
FOUR_YEAR_SMA = 1008
YEAR_SESSIONS = 252
ATR_PERIOD = 14

# A close under half (or over double) the prior close is a split whose history
# was stored unadjusted, or a collapse. Either way the long average straddling it
# says nothing about "stretched", so the name is set aside for that many sessions.
BREAK_RATIO = 0.5

MAX_CANDIDATES = 5
MAX_WATCHING = 5

# Study defaults.
STUDY_SAMPLES = 5
STUDY_SEED = 7
SIGNIFICANT_T = 2.0

SPY = "SPY"
VIX = "^VIX"

# What the study said when the rule shipped. Stated in the brief itself, because
# a list of names with stops and targets reads as advice unless it says otherwise.
# Re-run 'trd engine human study' and update this line; do not delete it.
MEASURED_NOTE = (
    "A screen, not a recommendation. MEASURED 2026-11-22 over 10 years (2,283 firings): "
    "no edge over a random name bought the same day (-0.006R, SE 0.041), and being below "
    "the 4-year average added nothing. It says where to look, not what to buy "
    "('trd engine human study')."
)


class StateSignal(BaseModel):
    """One market-state signal. `on` None = not enough data to say."""

    key: str
    name: str
    meaning: str
    on: bool | None
    detail: str


class MarketState(BaseModel):
    as_of: date | None = None
    spy_close: Decimal | None = None
    spy_vs_200_pct: Decimal | None = None
    spy_off_high_pct: Decimal | None = None
    vix: Decimal | None = None
    signals: list[StateSignal] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def on_count(self) -> int:
        return sum(1 for s in self.signals if s.on)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_count(self) -> int:
        return sum(1 for s in self.signals if s.on is not None)


class Candidate(BaseModel):
    """A name that is stretched and has turned, with the plan a buy would get."""

    symbol: str
    close: Decimal
    stretch_200_pct: Decimal
    stretch_4y_pct: Decimal | None = None  # None = under 1,008 sessions stored
    off_high_pct: Decimal | None = None
    trigger: str  # "reclaim" | "higher_low"
    reason: str
    stop_price: Decimal
    target_price: Decimal
    atr: Decimal
    quantity: Decimal | None = None  # None = no human sleeve to size from
    risk: Decimal | None = None  # dollars lost if the stop is hit
    earnings_on: date | None = None
    earnings_in_window: bool = False


class Watching(BaseModel):
    """Stretched, not turned yet."""

    symbol: str
    close: Decimal
    stretch_200_pct: Decimal
    stretch_4y_pct: Decimal | None = None
    off_high_pct: Decimal | None = None


class Excluded(BaseModel):
    """A name the rule picked that the owner cannot take, and why."""

    symbol: str
    why: str


class SleeveState(BaseModel):
    configured: bool = False
    slots: int = 0
    used: int = 0
    size: Decimal | None = None
    buying_power: Decimal | None = None
    halted: bool = False
    halt_reason: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def free(self) -> int:
        return max(0, self.slots - self.used)


class HumanBrief(BaseModel):
    on: date
    state: MarketState
    candidates: list[Candidate] = []
    watching: list[Watching] = []
    excluded: list[Excluded] = []
    sleeve: SleeveState = SleeveState()
    universe: int = 0
    screened: int = 0  # names with enough history for the rule
    short_history: int = 0
    stretched: int = 0  # names past the setup, turned or not
    with_4y: int = 0  # names with enough history for the 4-year column
    hold_sessions: int = 10
    hold_until: date | None = None
    caveats: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def market_open(self) -> bool:
        """Whether `on` is a stored session — the gate on sending anything."""
        return self.state.as_of == self.on


class Arm(BaseModel):
    """One side of the study, averaged. Count first."""

    count: int = 0
    forward_r: Decimal | None = None
    mfe_r: Decimal | None = None
    mae_r: Decimal | None = None
    target_first_pct: Decimal | None = None
    stop_first_pct: Decimal | None = None


class Paired(BaseModel):
    """A paired difference, rule minus control, in R."""

    label: str
    count: int = 0
    mean: Decimal | None = None
    se: Decimal | None = None
    t: Decimal | None = None


class StudyReport(BaseModel):
    horizon: int
    samples: int
    seed: int
    names: int = 0
    first: date | None = None
    last: date | None = None
    rule: Arm = Arm()
    control: Arm = Arm()
    edge: Paired = Paired(label="forward R")
    net_edge: Paired = Paired(label="net (MFE + MAE)")
    halves: list[Paired] = []
    by_trigger: list[Paired] = []
    four_year: list[Paired] = []
    significant: bool = False
    caveats: list[str] = []


# --------------------------------------------------------------- pure helpers


def _dec(value: float | None, places: str = "0.01") -> Decimal | None:
    if value is None or math.isnan(value):
        return None
    return Decimal(str(value)).quantize(Decimal(places))


def _argmin_last(values: Sequence[float]) -> int:
    """Index of the lowest value; the LAST one on a tie, so a retest that
    matches the low exactly counts as the low."""
    best = 0
    for i, v in enumerate(values):
        if v <= values[best]:
            best = i
    return best


def _argmax_first(values: Sequence[float]) -> int:
    best = 0
    for i, v in enumerate(values):
        if v > values[best]:
            best = i
    return best


class Series:
    """One name's daily bars with the series the rule reads, computed once."""

    def __init__(self, bars: Sequence[DailyBar]) -> None:
        self.bars = list(bars)
        self.close = [float(b.close) for b in self.bars]
        self.high = [float(b.high) for b in self.bars]
        self.low = [float(b.low) for b in self.bars]
        self.index = {b.date: i for i, b in enumerate(self.bars)}
        self.sma_turn = indicator("sma", self.bars, period=TURN_SMA)["value"]
        self.sma_long = indicator("sma", self.bars, period=LONG_SMA)["value"]
        self.atr = indicator("atr", self.bars, period=ATR_PERIOD)["value"]
        self._sma_4y: list[float | None] | None = None
        self.breaks = [
            k
            for k in range(1, len(self.close))
            if self.close[k - 1] > 0
            and not BREAK_RATIO <= self.close[k] / self.close[k - 1] <= 1 / BREAK_RATIO
        ]

    def break_in_window(self, i: int) -> int | None:
        """Index of the newest price break the long average at `i` still spans."""
        for k in reversed(self.breaks):
            if k <= i:
                return k if i - k < LONG_SMA else None
        return None

    def __len__(self) -> int:
        return len(self.bars)

    def stretch(self, i: int) -> float | None:
        """Close against the long average, as a fraction (-0.12 = 12% below)."""
        avg = self.sma_long[i] if i < len(self.sma_long) else None
        return None if avg is None or avg <= 0 else self.close[i] / avg - 1

    def stretch_4y(self, i: int) -> float | None:
        if len(self.bars) < FOUR_YEAR_SMA:
            return None
        if self._sma_4y is None:
            self._sma_4y = indicator("sma", self.bars, period=FOUR_YEAR_SMA)["value"]
        avg = self._sma_4y[i]
        return None if avg is None or avg <= 0 else self.close[i] / avg - 1

    def off_high(self, i: int) -> float | None:
        lo = max(0, i - YEAR_SESSIONS + 1)
        peak = max(self.high[lo : i + 1], default=0.0)
        return None if peak <= 0 else self.close[i] / peak - 1


def higher_low_at(s: Series, i: int) -> bool | None:
    """A low at least HIGHER_LOW_MIN_SESSIONS old, a later low that held above
    it, and a close through the prior session's high. None = too little history."""
    if i < LOOKBACK:
        return None
    lo = i - LOOKBACK + 1
    k = lo + _argmin_last(s.low[lo : i + 1])
    if i - k < HIGHER_LOW_MIN_SESSIONS:
        return False
    return min(s.low[i - 1], s.low[i]) > s.low[k] and s.close[i] > s.high[i - 1]


def reclaim_at(s: Series, i: int) -> bool:
    """Closed above the short average, having closed at or below it within
    RECLAIM_WINDOW sessions."""
    now = s.sma_turn[i]
    if now is None or s.close[i] <= now:
        return False
    for back in range(1, RECLAIM_WINDOW + 1):
        j = i - back
        if j < 0:
            break
        then = s.sma_turn[j]
        if then is not None and s.close[j] <= then:
            return True
    return False


def fires(s: Series, i: int) -> str | None:
    """The candidate rule at bar `i`: the trigger that fired, or None.

    One function for the live screen and the study, so what is measured is
    what is shown.
    """
    stretch = s.stretch(i)
    if stretch is None or stretch * 100 > -STRETCH_MIN_PCT:
        return None
    if s.break_in_window(i) is not None:
        return None
    if reclaim_at(s, i):
        return "reclaim"
    if higher_low_at(s, i):
        return "higher_low"
    return None


def sessions_ahead(start: date, sessions: int) -> date:
    """The date `sessions` trading sessions after `start`, skipping weekends
    and the market holidays the calendar knows."""
    d = start
    left = sessions
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in MARKET_HOLIDAYS:
            left -= 1
    return d


# ------------------------------------------------------- market-state signals


def vix_rollover(spy: Series, vix: Series) -> StateSignal:
    name, key = "VIX rolls over", "vix_rollover"
    meaning = "The index makes a new low but fear (VIX) does not make a new high."
    dates = [b.date for b in spy.bars if b.date in vix.index][-LOOKBACK:]
    if len(dates) < LOOKBACK:
        return StateSignal(key=key, name=name, meaning=meaning, on=None, detail="not enough bars")
    closes = [spy.close[spy.index[d]] for d in dates]
    fear = [vix.close[vix.index[d]] for d in dates]
    low_i = _argmin_last(closes)
    peak_i = _argmax_first(fear)
    age = len(dates) - 1 - low_i
    if age >= FRESH_LOW_SESSIONS:
        return StateSignal(
            key=key,
            name=name,
            meaning=meaning,
            on=False,
            detail=f"no fresh index low: the {LOOKBACK}-session low is {age} sessions old",
        )
    on = peak_i < low_i and fear[low_i] < fear[peak_i]
    detail = (
        f"index low {dates[low_i]:%m-%d} with VIX {fear[low_i]:.1f}; "
        f"VIX peak {fear[peak_i]:.1f} on {dates[peak_i]:%m-%d}"
    )
    return StateSignal(key=key, name=name, meaning=meaning, on=on, detail=detail)


def capitulation_bar(spy: Series) -> StateSignal:
    name, key = "Capitulation bar", "capitulation"
    meaning = "A gap down that closes green on heavy volume: forced sellers finished."
    n = len(spy)
    if n < LOOKBACK + CAPITULATION_WINDOW + 1:
        return StateSignal(key=key, name=name, meaning=meaning, on=None, detail="not enough bars")
    seen_volume = False
    for i in range(n - 1, n - 1 - CAPITULATION_WINDOW, -1):
        bar, prev = spy.bars[i], spy.bars[i - 1]
        volumes = [b.volume for b in spy.bars[i - LOOKBACK : i] if b.volume]
        if not bar.volume or not volumes:
            continue
        seen_volume = True
        gap = (float(bar.open) / float(prev.close) - 1) * 100
        multiple = bar.volume / (sum(volumes) / len(volumes))
        if gap <= -GAP_DOWN_PCT and bar.close > bar.open and multiple >= HEAVY_VOLUME_MULTIPLE:
            return StateSignal(
                key=key,
                name=name,
                meaning=meaning,
                on=True,
                detail=f"{bar.date:%m-%d}: gapped {gap:+.1f}%, closed green, "
                f"{multiple:.1f}x average volume",
            )
    if not seen_volume:
        return StateSignal(key=key, name=name, meaning=meaning, on=None, detail="no volume stored")
    return StateSignal(
        key=key,
        name=name,
        meaning=meaning,
        on=False,
        detail=f"none in the last {CAPITULATION_WINDOW} sessions",
    )


def higher_low(spy: Series) -> StateSignal:
    name, key = "Higher low", "higher_low"
    meaning = "A second sell-off holds above the first low, then price takes out a prior high."
    i = len(spy) - 1
    on = higher_low_at(spy, i) if i >= 0 else None
    if on is None:
        return StateSignal(key=key, name=name, meaning=meaning, on=None, detail="not enough bars")
    lo = i - LOOKBACK + 1
    k = lo + _argmin_last(spy.low[lo : i + 1])
    age = i - k
    if age < HIGHER_LOW_MIN_SESSIONS:
        when = "the last session" if age == 0 else f"{age} session(s) ago"
        detail = (
            f"the low ({spy.low[k]:.2f}) was made {when}; it needs "
            f"{HIGHER_LOW_MIN_SESSIONS} sessions behind it"
        )
    else:
        detail = (
            f"low {spy.low[k]:.2f} on {spy.bars[k].date:%m-%d}; last two sessions held "
            f"{min(spy.low[i - 1], spy.low[i]):.2f}; close {spy.close[i]:.2f} vs prior high "
            f"{spy.high[i - 1]:.2f}"
        )
    return StateSignal(key=key, name=name, meaning=meaning, on=on, detail=detail)


def _advancing(names: Sequence[Series], day: date) -> tuple[int, int]:
    up = total = 0
    for s in names:
        i = s.index.get(day)
        if i is None or i == 0:
            continue
        total += 1
        up += s.close[i] > s.close[i - 1]
    return up, total


def breadth_thrust(spy: Series, names: Sequence[Series]) -> StateSignal:
    name, key = "Breadth thrust", "breadth_thrust"
    meaning = (
        "Breadth is the share of stocks closing up. A washout (almost none) followed by "
        "a day when almost all rise."
    )
    days = [b.date for b in spy.bars][-BREADTH_WINDOW:]
    rows: list[tuple[date, float]] = []
    for day in days:
        up, total = _advancing(names, day)
        if total >= MIN_BREADTH_NAMES:
            rows.append((day, up / total * 100))
    if len(rows) < BREADTH_WINDOW // 2:
        return StateSignal(
            key=key, name=name, meaning=meaning, on=None, detail="too few names with bars"
        )
    washout = next(((d, p) for d, p in rows if p <= WASHOUT_PCT), None)
    thrust = (
        next(((d, p) for d, p in rows if d > washout[0] and p >= THRUST_PCT), None)
        if washout
        else None
    )
    low_day, low_pct = min(rows, key=lambda r: r[1])
    last_day, last_pct = rows[-1]
    if washout and thrust:
        detail = (
            f"washout {washout[1]:.0f}% up on {washout[0]:%m-%d}, "
            f"thrust {thrust[1]:.0f}% on {thrust[0]:%m-%d}"
        )
    elif washout:
        detail = (
            f"washout {washout[1]:.0f}% up on {washout[0]:%m-%d}, no {THRUST_PCT:.0f}% day "
            f"since (last {last_pct:.0f}% on {last_day:%m-%d})"
        )
    else:
        detail = (
            f"no washout: weakest day {low_pct:.0f}% up on {low_day:%m-%d} "
            f"(needs {WASHOUT_PCT:.0f}% or less)"
        )
    return StateSignal(
        key=key, name=name, meaning=meaning, on=bool(washout and thrust), detail=detail
    )


def leadership_flip(spy: Series, names: Sequence[Series]) -> StateSignal:
    name, key = "Leadership flip", "leadership_flip"
    meaning = "On an up day, the names that fell hardest rise more than the ones that held up."
    i = len(spy) - 1
    if i < 1:
        return StateSignal(key=key, name=name, meaning=meaning, on=None, detail="not enough bars")
    day = spy.bars[i].date
    if spy.close[i] <= spy.close[i - 1]:
        return StateSignal(
            key=key,
            name=name,
            meaning=meaning,
            on=False,
            detail=f"{day:%m-%d} was not an up day for the index",
        )
    rows: list[tuple[float, float]] = []  # (prior return, day return)
    for s in names:
        j = s.index.get(day)
        if j is None or j < LEADERSHIP_SESSIONS + 1:
            continue
        prior = s.close[j - 1] / s.close[j - 1 - LEADERSHIP_SESSIONS] - 1
        rows.append((prior, s.close[j] / s.close[j - 1] - 1))
    if len(rows) < MIN_BREADTH_NAMES:
        return StateSignal(
            key=key, name=name, meaning=meaning, on=None, detail="too few names with bars"
        )
    rows.sort()
    fifth = max(1, len(rows) // 5)
    beaten = sum(r[1] for r in rows[:fifth]) / fifth * 100
    strong = sum(r[1] for r in rows[-fifth:]) / fifth * 100
    return StateSignal(
        key=key,
        name=name,
        meaning=meaning,
        on=beaten > strong,
        detail=f"{day:%m-%d}: hardest-hit fifth {beaten:+.1f}%, strongest fifth {strong:+.1f}%",
    )


def rsi_divergence(spy: Series) -> StateSignal:
    name, key = "RSI divergence", "rsi_divergence"
    meaning = (
        "RSI is a 0-100 momentum gauge. Price makes a lower low while RSI makes a higher "
        "low: the selling is losing force."
    )
    n = len(spy)
    rsi = indicator("rsi", spy.bars, period=RSI_PERIOD)["value"]
    if n < RSI_PRIOR + RSI_PERIOD:
        return StateSignal(key=key, name=name, meaning=meaning, on=None, detail="not enough bars")
    recent_lo = n - RSI_RECENT
    prior_lo = n - RSI_PRIOR
    r = recent_lo + _argmin_last(spy.close[recent_lo:])
    p = prior_lo + _argmin_last(spy.close[prior_lo:recent_lo])
    rsi_r, rsi_p = rsi[r], rsi[p]
    if rsi_r is None or rsi_p is None:
        return StateSignal(key=key, name=name, meaning=meaning, on=None, detail="not enough bars")
    if spy.close[r] >= spy.close[p]:
        return StateSignal(
            key=key,
            name=name,
            meaning=meaning,
            on=False,
            detail=f"no lower low: {spy.close[r]:.2f} vs {spy.close[p]:.2f} "
            f"on {spy.bars[p].date:%m-%d}",
        )
    return StateSignal(
        key=key,
        name=name,
        meaning=meaning,
        on=rsi_r > rsi_p,
        detail=f"price {spy.close[p]:.2f} to {spy.close[r]:.2f}, RSI {rsi_p:.0f} to {rsi_r:.0f}",
    )


def market_state(spy: Series | None, vix: Series | None, names: Sequence[Series]) -> MarketState:
    if spy is None or not len(spy):
        return MarketState()
    i = len(spy) - 1
    missing = StateSignal(
        key="vix_rollover",
        name="VIX rolls over",
        meaning="The index makes a new low but fear (VIX) does not make a new high.",
        on=None,
        detail="no VIX bars stored",
    )
    signals = [
        vix_rollover(spy, vix) if vix is not None and len(vix) else missing,
        capitulation_bar(spy),
        higher_low(spy),
        breadth_thrust(spy, names),
        leadership_flip(spy, names),
        rsi_divergence(spy),
    ]
    stretch = spy.stretch(i)
    off = spy.off_high(i)
    return MarketState(
        as_of=spy.bars[i].date,
        spy_close=spy.bars[i].close,
        spy_vs_200_pct=_dec(stretch * 100 if stretch is not None else None),
        spy_off_high_pct=_dec(off * 100 if off is not None else None),
        vix=vix.bars[-1].close if vix is not None and len(vix) else None,
        signals=signals,
    )


# ------------------------------------------------------------------- service


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _paired(label: str, diffs: Sequence[float]) -> Paired:
    n = len(diffs)
    if n == 0:
        return Paired(label=label)
    mean = sum(diffs) / n
    if n < 2:
        return Paired(label=label, count=n, mean=_dec(mean, "0.001"))
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(var / n)
    return Paired(
        label=label,
        count=n,
        mean=_dec(mean, "0.001"),
        se=_dec(se, "0.001"),
        t=_dec(mean / se, "0.01") if se > 0 else None,
    )


class _Walk(BaseModel):
    forward: float
    mfe: float
    mae: float
    resolved: str | None


class HumanBriefService:
    """Reads one engine home. Give it a read-only connection."""

    def __init__(self, engine: EngineService) -> None:
        self.engine = engine
        self.source = BarSource.stamper(DAILY)

    # ---------------------------------------------------------------- loading

    def _series(self, instrument: Instrument, on: date | None) -> Series:
        bars = self.engine.prices.daily_bars(instrument.id)
        if on is not None:
            bars = [b for b in bars if b.date <= on]
        return Series(bars)

    def _by_symbol(self, symbol: str, on: date | None) -> Series | None:
        instrument = self.engine.instruments.get_by_symbol(symbol)
        return self._series(instrument, on) if instrument is not None else None

    # ------------------------------------------------------------------ brief

    def brief(self, on: date) -> HumanBrief:
        config = self.engine.config()
        account = self.engine.account()
        universe = self.engine.universe()
        loaded = [(inst, self._series(inst, on)) for inst in universe]
        names = [s for _, s in loaded if len(s) > 1]
        state = market_state(self._by_symbol(SPY, on), self._by_symbol(VIX, on), names)

        sleeve = self._sleeve(on)
        hold = int(config.exit_params.get("max_sessions", 10))
        as_of = state.as_of or on
        hold_until = sessions_ahead(as_of, hold)

        owners: dict[str, str] = {}
        for position, held in self.engine.positions.list_open(account.id):
            owners[held.symbol] = (
                "already in a human slot" if is_human(position.strategy) else "held by the engine"
            )
        for intent, wanted in self.engine.intents.list_open(account.id):
            owners.setdefault(wanted.symbol, f"a {intent.side.value} order is in flight")

        candidates: list[Candidate] = []
        watching: list[Watching] = []
        excluded: list[Excluded] = []
        screened = short = stretched = with_4y = 0
        for instrument, s in loaded:
            i = len(s) - 1
            if i < 0 or s.bars[i].date != as_of or s.stretch(i) is None:
                short += 1
                continue
            screened += 1
            with_4y += len(s) >= FOUR_YEAR_SMA
            stretch = s.stretch(i)
            assert stretch is not None
            if stretch * 100 > -STRETCH_MIN_PCT:
                continue
            broke = s.break_in_window(i)
            if broke is not None:
                move = (s.close[broke] / s.close[broke - 1] - 1) * 100
                excluded.append(
                    Excluded(
                        symbol=instrument.symbol,
                        why=f"price break {s.bars[broke].date:%Y-%m-%d} ({move:+.0f}% in one "
                        "session): a split stored unadjusted or a collapse — not screened",
                    )
                )
                continue
            stretched += 1
            four = s.stretch_4y(i)
            off = s.off_high(i)
            stretch_pct = _dec(stretch * 100)
            assert stretch_pct is not None
            trigger = fires(s, i)
            if trigger is None:
                watching.append(
                    Watching(
                        symbol=instrument.symbol,
                        close=s.bars[i].close,
                        stretch_200_pct=stretch_pct,
                        stretch_4y_pct=_dec(four * 100 if four is not None else None),
                        off_high_pct=_dec(off * 100 if off is not None else None),
                    )
                )
                continue
            if instrument.symbol in owners:
                excluded.append(Excluded(symbol=instrument.symbol, why=owners[instrument.symbol]))
                continue
            candidate = self._candidate(instrument, s, trigger, sleeve, config.exit_params)
            if candidate is None:
                excluded.append(Excluded(symbol=instrument.symbol, why="no ATR to size a stop"))
                continue
            earnings = self.engine.earnings.next_for_instrument(instrument.id, start=as_of)
            candidate.earnings_on = earnings
            candidate.earnings_in_window = earnings is not None and earnings <= hold_until
            candidates.append(candidate)

        candidates.sort(key=lambda c: c.stretch_200_pct)
        watching.sort(key=lambda w: w.stretch_200_pct)

        caveats = [
            MEASURED_NOTE,
            "Stretched means below its own 200-session average; in a broad sell-off most "
            "names are, so the turn matters more than the stretch.",
        ]
        if state.as_of is not None and state.as_of != on:
            caveats.append(
                f"No session stored for {on:%Y-%m-%d}; read from {state.as_of:%Y-%m-%d}."
            )
        return HumanBrief(
            on=on,
            state=state,
            candidates=candidates[:MAX_CANDIDATES],
            watching=watching[:MAX_WATCHING],
            excluded=excluded,
            sleeve=sleeve,
            universe=len(universe),
            screened=screened,
            short_history=short,
            stretched=stretched,
            with_4y=with_4y,
            hold_sessions=hold,
            hold_until=hold_until,
            caveats=caveats,
        )

    def _sleeve(self, on: date) -> SleeveState:
        config = self.engine.config()
        account = self.engine.account()
        row = self.engine.humans.get(account.id) if config.is_live else None
        if row is None:
            return SleeveState()
        used = sum(
            1 for p, _ in self.engine.positions.list_open(account.id) if is_human(p.strategy)
        ) + sum(
            1
            for intent, _ in self.engine.intents.list_open(account.id)
            if intent.side == Side.BUY and is_human(intent.strategy)
        )
        cash = self.engine.human_cash(on)
        return SleeveState(
            configured=True,
            slots=row.slots,
            used=used,
            size=row.size,
            buying_power=(
                cash.buying_power.quantize(Decimal("0.01"), ROUND_DOWN)
                if cash is not None
                else None
            ),
            halted=row.halted,
            halt_reason=row.halt_reason,
        )

    def _candidate(
        self,
        instrument: Instrument,
        s: Series,
        trigger: str,
        sleeve: SleeveState,
        exit_params: dict[str, float],
    ) -> Candidate | None:
        i = len(s) - 1
        price = s.bars[i].close
        atr = s.atr[i]
        if atr is None or atr <= 0:
            return None
        atr_dec = Decimal(str(atr))
        quantity = risk = None
        stop, target = entry_levels(price, atr_dec, exit_params)
        if sleeve.configured and sleeve.size:
            plan, _ = plan_entry(s.bars, sleeve.size, exit_params, SizingMode.EXPOSURE)
            if plan is not None:
                quantity, stop, target = plan.quantity, plan.stop_price, plan.target_price
                risk = (quantity * (price - stop)).quantize(Decimal("0.01"))
        if stop <= 0:
            return None
        stretch = s.stretch(i)
        four = s.stretch_4y(i)
        off = s.off_high(i)
        assert stretch is not None
        turned = (
            f"closed back above its {TURN_SMA}-session average"
            if trigger == "reclaim"
            else "held a higher low and closed through the prior session's high"
        )
        reason = f"{abs(stretch) * 100:.0f}% below its {LONG_SMA}-session average and {turned}"
        stretch_pct = _dec(stretch * 100)
        assert stretch_pct is not None
        return Candidate(
            symbol=instrument.symbol,
            close=price,
            stretch_200_pct=stretch_pct,
            stretch_4y_pct=_dec(four * 100 if four is not None else None),
            off_high_pct=_dec(off * 100 if off is not None else None),
            trigger=trigger,
            reason=reason,
            stop_price=stop.quantize(Decimal("0.01")),
            target_price=target.quantize(Decimal("0.01")),
            atr=atr_dec.quantize(Decimal("0.0001")),
            quantity=quantity,
            risk=risk,
        )

    # ------------------------------------------------------------------ study

    def _walk(self, s: Series, i: int, horizon: int, exit_params: dict[str, float]) -> _Walk | None:
        """What an entry at bar `i`'s close did over the next `horizon` sessions,
        in R, with R the stop distance the engine would have set."""
        atr = s.atr[i] if i < len(s.atr) else None
        ahead = s.bars[i + 1 : i + 1 + horizon]
        if atr is None or atr <= 0 or len(ahead) < horizon:
            return None
        entry = s.bars[i].close
        risk = stop_distance(Decimal(str(atr)), exit_params)
        if risk <= 0 or entry <= 0:
            return None
        ex = excursion(ahead, entry, risk, self.source)
        if ex.mfe_r is None or ex.mae_r is None:
            return None
        return _Walk(
            forward=float((ahead[-1].close - entry) / risk),
            mfe=float(ex.mfe_r),
            mae=float(ex.mae_r),
            resolved=resolution(ahead, entry, risk),
        )

    def study(
        self,
        horizon: int | None = None,
        samples: int = STUDY_SAMPLES,
        seed: int = STUDY_SEED,
    ) -> StudyReport:
        """Does the candidate rule beat a random name on the same session?

        Every FRESH firing (fires today, did not yesterday) in every universe
        name, paired with `samples` other names drawn at random on the same
        date. Same date on purpose: after a sell-off everything bounces, and a
        rule that only found the bounce has found nothing.
        """
        config = self.engine.config()
        params = config.exit_params
        hold = horizon or int(params.get("max_sessions", 10))
        rng = random.Random(seed)
        loaded = [(inst.symbol, self._series(inst, None)) for inst in self.engine.universe()]
        loaded = [(sym, s) for sym, s in loaded if len(s) > LONG_SMA + hold]
        cache: dict[tuple[str, int], _Walk | None] = {}

        def walk(symbol: str, s: Series, i: int) -> _Walk | None:
            key = (symbol, i)
            if key not in cache:
                cache[key] = self._walk(s, i, hold, params)
            return cache[key]

        # (date, trigger, has 4y stretch below zero, rule walk, control walks)
        events: list[tuple[date, str, bool | None, _Walk, list[_Walk]]] = []
        for symbol, s in loaded:
            for i in range(LONG_SMA, len(s) - hold):
                trigger = fires(s, i)
                if trigger is None or fires(s, i - 1) is not None:
                    continue
                mine = walk(symbol, s, i)
                if mine is None:
                    continue
                day = s.bars[i].date
                pool = [(o, t) for o, t in loaded if o != symbol and day in t.index]
                rng.shuffle(pool)
                controls: list[_Walk] = []
                for other, t in pool:
                    found = walk(other, t, t.index[day])
                    if found is not None:
                        controls.append(found)
                    if len(controls) == samples:
                        break
                if not controls:
                    continue
                four = s.stretch_4y(i)
                events.append((day, trigger, None if four is None else four < 0, mine, controls))

        report = StudyReport(
            horizon=hold,
            samples=samples,
            seed=seed,
            names=len(loaded),
            caveats=[
                "Today's universe replayed over its own past: names that were delisted or "
                "never added are missing (survivorship).",
                "Forward R is the close after the horizon with no stop applied; read "
                "'target first' against 'stop first' for what a stopped trade would have seen.",
                "Entries at the signal bar's close. A human buy is placed the next session, "
                "at a different price.",
            ],
        )
        if not events:
            report.caveats.insert(0, "The rule never fired in the stored history.")
            return report
        events.sort(key=lambda e: e[0])

        def arm(walks: Sequence[_Walk]) -> Arm:
            n = len(walks)
            return Arm(
                count=n,
                forward_r=_dec(_mean([w.forward for w in walks]), "0.001"),
                mfe_r=_dec(_mean([w.mfe for w in walks]), "0.001"),
                mae_r=_dec(_mean([w.mae for w in walks]), "0.001"),
                target_first_pct=_dec(sum(w.resolved == "target" for w in walks) / n * 100, "0.1"),
                stop_first_pct=_dec(sum(w.resolved == "stop" for w in walks) / n * 100, "0.1"),
            )

        def diff(event: tuple[date, str, bool | None, _Walk, list[_Walk]]) -> float:
            control = _mean([w.forward for w in event[4]])
            assert control is not None
            return event[3].forward - control

        def net_diff(event: tuple[date, str, bool | None, _Walk, list[_Walk]]) -> float:
            control = _mean([w.mfe + w.mae for w in event[4]])
            assert control is not None
            return event[3].mfe + event[3].mae - control

        report.first, report.last = events[0][0], events[-1][0]
        report.rule = arm([e[3] for e in events])
        report.control = arm([w for e in events for w in e[4]])
        report.edge = _paired("forward R", [diff(e) for e in events])
        report.net_edge = _paired("net (MFE + MAE)", [net_diff(e) for e in events])
        mid = len(events) // 2
        report.halves = [
            _paired(
                f"first half (to {events[mid - 1][0]:%Y-%m-%d})", [diff(e) for e in events[:mid]]
            )
            if mid
            else Paired(label="first half"),
            _paired(
                f"second half (from {events[mid][0]:%Y-%m-%d})", [diff(e) for e in events[mid:]]
            ),
        ]
        report.by_trigger = [
            _paired(trigger, [diff(e) for e in events if e[1] == trigger])
            for trigger in ("reclaim", "higher_low")
        ]
        with_four = [e for e in events if e[2] is not None]
        if with_four:
            report.four_year = [
                _paired("below its 4-year average", [diff(e) for e in with_four if e[2]]),
                _paired("above its 4-year average", [diff(e) for e in with_four if not e[2]]),
            ]
        else:
            report.caveats.append(
                f"No name has the {FOUR_YEAR_SMA:,} sessions the 4-year average needs, so "
                "whether it is selective is still unanswered here."
            )
        t = report.edge.t
        halves = [h.mean for h in report.halves]
        report.significant = bool(
            t is not None
            and abs(t) >= Decimal(str(SIGNIFICANT_T))
            and all(h is not None for h in halves)
            and len({h > 0 for h in halves if h is not None}) == 1
        )
        return report


__all__ = [
    "Arm",
    "Candidate",
    "Excluded",
    "HumanBrief",
    "HumanBriefService",
    "MarketState",
    "Paired",
    "Series",
    "SleeveState",
    "StateSignal",
    "StudyReport",
    "Watching",
    "fires",
    "higher_low_at",
    "market_state",
    "reclaim_at",
    "sessions_ahead",
]
