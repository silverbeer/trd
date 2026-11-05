"""Market temperature: one read of what the whole market is doing today (SB-1538).

Every entry rule asks about one name. The regime gate (`trd.engine.regime`)
asks about the trend — SPY against its N-day average, VIX against a ceiling —
and is off by default. Neither says anything about *today*: the morning after
an election, with SPY up 1.6% and nearly everything green, looks to the engine
exactly like a quiet Tuesday.

This is that missing read. Three inputs, all from the quotes a scan already
fetches:

- the S&P 500 (SPY) move from the prior close
- breadth: the share of the engine's own universe trading up on the day
- VIX: its level, and its move from the prior close

They fold into one label — cold, cool, neutral, warm, hot — by fixed
thresholds below. Module constants, not config: a temperature someone tunes
until it agrees with them is an opinion with extra steps.

It decides nothing. It is recorded with every scan, shown in the reports, and
is an input a later rule may read (the rotation rule, SB-1539) — only once a
backtest has said it helps.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from trd.engine.regime import TREND_SYMBOL, VIX_SYMBOL
from trd.models import MarketTemperature, Quote, Temperature

# SPY's day move, percent. A 1% S&P day is a big day; 0.4% is a clear lean.
HOT_SPY_PCT = Decimal("1.0")
WARM_SPY_PCT = Decimal("0.4")
# Breadth, percent of the universe up on the day.
HOT_BREADTH_PCT = Decimal("70")
WARM_BREADTH_PCT = Decimal("60")
# A VIX jump this large (percent) caps the day at 'cool' whatever SPY did: fear
# rising fast is not a warm market even on a green tape.
VIX_SPIKE_PCT = Decimal("10")
# Breadth over fewer names than this is not a market read.
MIN_BREADTH_NAMES = 5


def _pct(quote: Quote | None) -> Decimal | None:
    if quote is None or quote.prev_close is None or quote.prev_close <= 0:
        return None
    return (quote.price / quote.prev_close - 1) * 100


def _sign(value: Decimal | None, hot: Decimal, warm: Decimal, centre: Decimal = Decimal(0)) -> int:
    """+2 hot, +1 warm, 0 neutral, -1 cool, -2 cold — symmetric about `centre`."""
    if value is None:
        return 0
    d = value - centre
    if d >= hot - centre:
        return 2
    if d >= warm - centre:
        return 1
    if d <= -(hot - centre):
        return -2
    if d <= -(warm - centre):
        return -1
    return 0


def market_temperature(quotes: dict[str, Quote], universe: Iterable[str]) -> MarketTemperature:
    """The day's temperature from one pass's quotes.

    `universe` names the engine's own symbols — breadth is measured over them,
    never over SPY or VIX. Missing inputs count as neutral rather than
    guessed: no SPY quote and too few names for breadth is 'neutral, no data',
    said in the reason.
    """
    spy = _pct(quotes.get(TREND_SYMBOL))
    vix_quote = quotes.get(VIX_SYMBOL)
    vix = vix_quote.price if vix_quote is not None else None
    vix_pct = _pct(vix_quote)

    moves = [
        m
        for s in universe
        if s not in (TREND_SYMBOL, VIX_SYMBOL) and (m := _pct(quotes.get(s))) is not None
    ]
    breadth = (
        Decimal(sum(1 for m in moves if m > 0)) / Decimal(len(moves)) * 100
        if len(moves) >= MIN_BREADTH_NAMES
        else None
    )

    spy_score = _sign(spy, HOT_SPY_PCT, WARM_SPY_PCT)
    # Breadth is centred on 50%: 70% up is as hot as 30% up is cold.
    breadth_score = _sign(breadth, HOT_BREADTH_PCT, WARM_BREADTH_PCT, Decimal(50))
    scores = [s for s, v in ((spy_score, spy), (breadth_score, breadth)) if v is not None]

    if not scores:
        return MarketTemperature(
            label=Temperature.NEUTRAL,
            vix=vix,
            vix_pct=vix_pct,
            reason="no S&P quote and too few names for breadth — no read",
        )
    # Hot or cold needs every input that is present to agree at full strength;
    # one strong reading against a lukewarm other is only warm or cool.
    if all(s == 2 for s in scores):
        label = Temperature.HOT
    elif all(s == -2 for s in scores):
        label = Temperature.COLD
    elif sum(scores) > 0 and all(s >= 0 for s in scores):
        label = Temperature.WARM
    elif sum(scores) < 0 and all(s <= 0 for s in scores):
        label = Temperature.COOL
    else:
        label = Temperature.NEUTRAL
    reason = f"S&P and breadth agree on {label.value}"
    if len(scores) == 1:
        reason = f"{'S&P' if spy is not None else 'breadth'} alone reads {label.value}"
    if (
        vix_pct is not None
        and vix_pct >= VIX_SPIKE_PCT
        and label
        in (
            Temperature.HOT,
            Temperature.WARM,
            Temperature.NEUTRAL,
        )
    ):
        label = Temperature.COOL
        reason = f"VIX up {vix_pct:.0f}% — fear rising caps the day at cool"
    elif label == Temperature.NEUTRAL and len(scores) == 2 and spy_score * breadth_score < 0:
        reason = "S&P and breadth disagree"
    return MarketTemperature(
        label=label,
        spy_pct=spy,
        breadth_pct=breadth,
        breadth_names=len(moves),
        vix=vix,
        vix_pct=vix_pct,
        reason=reason,
    )
