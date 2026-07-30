"""Exit rules.

Getting out is the half most paper traders skip, so every open position is
checked against all five rules on every scan, in a fixed order: the ones that
protect capital run before the ones that book profit.

    stop -> trail -> target -> indicator -> time

The initial stop never moves. The trailing stop rides up behind the highest price
the trade has seen and only takes over once it sits above the initial stop, which
is what turns a winner into a "can't lose money now" trade without ever loosening
the original risk.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel

from trd.engine.base import indicator, last, prior
from trd.models import DailyBar, EnginePosition

DEFAULT_EXIT_PARAMS: dict[str, float] = {
    "stop_atr_mult": 2.0,  # initial stop = entry - N x ATR(14)
    "target_r": 2.0,  # profit target = entry + N x initial risk
    "trail_atr_mult": 3.0,  # chandelier stop = highest close since entry - N x ATR
    "max_bars": 10.0,  # give up on a trade that has gone nowhere in N bars
    "rsi_exit": 80.0,  # blow-off exit when RSI runs this hot and rolls over
    "indicator_grace_bars": 3.0,  # let a new entry breathe before indicator exits apply
    # Flat-by-the-bell, as HHMM in the engine's local time. 0 disables it, which is
    # what a swing engine wants: its whole design is to carry positions overnight.
    "flat_at_minute": 0.0,
}


class ExitDecision(BaseModel):
    rule: str
    reason: str


class ExitRule(ABC):
    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def check(
        self,
        position: EnginePosition,
        bars: list[DailyBar],
        price: Decimal,
        params: dict[str, float],
        now: datetime,
    ) -> ExitDecision | None:
        """Return a decision if this rule says get out, else None."""


class StopLoss(ExitRule):
    key = "stop"
    name = "Stop Loss"
    description = (
        "Fixed exit at entry minus N x ATR. Volatility-scaled, so a jumpy name gets "
        "a wider stop than a calm one and both risk the same dollars."
    )

    def check(
        self,
        position: EnginePosition,
        bars: list[DailyBar],
        price: Decimal,
        params: dict[str, float],
        now: datetime,
    ) -> ExitDecision | None:
        if price > position.stop_price:
            return None
        return ExitDecision(
            rule=self.key,
            reason=(
                f"hit the stop at {position.stop_price:.2f} — thesis broke, "
                f"lost 1R ({position.risk_per_share:.2f}/share)"
            ),
        )


class TrailingStop(ExitRule):
    key = "trail"
    name = "Trailing Stop"
    description = (
        "Chandelier stop: highest close since entry minus N x ATR. Only takes over "
        "once it sits above the initial stop, so it locks in gains without ever "
        "widening the original risk."
    )

    def check(
        self,
        position: EnginePosition,
        bars: list[DailyBar],
        price: Decimal,
        params: dict[str, float],
        now: datetime,
    ) -> ExitDecision | None:
        mult = Decimal(str(params.get("trail_atr_mult", 3.0)))
        trail_stop = position.trail_high - position.atr_at_entry * mult
        if trail_stop <= position.stop_price:
            return None  # initial stop is still the tighter of the two
        if price > trail_stop:
            return None
        given_back = position.trail_high - price
        return ExitDecision(
            rule=self.key,
            reason=(
                f"trailed out at {trail_stop:.2f} after peaking at {position.trail_high:.2f} — "
                f"gave back {given_back:.2f}/share, kept the rest"
            ),
        )


class ProfitTarget(ExitRule):
    key = "target"
    name = "Profit Target"
    description = (
        "Exit at entry plus N x the initial risk. A 2R target with a 50% win rate "
        "is a profitable system; that is the whole arithmetic of the thing."
    )

    def check(
        self,
        position: EnginePosition,
        bars: list[DailyBar],
        price: Decimal,
        params: dict[str, float],
        now: datetime,
    ) -> ExitDecision | None:
        if price < position.target_price:
            return None
        r = params.get("target_r", 2.0)
        return ExitDecision(
            rule=self.key,
            reason=f"hit the {r:.0f}R target at {position.target_price:.2f} — took the win",
        )


class IndicatorExit(ExitRule):
    key = "indicator"
    name = "Indicator Exit"
    description = (
        "Leave when the reason for owning it stops being true: price loses the "
        "20-day, MACD momentum flips negative, or RSI runs above the blow-off "
        "level and rolls over. Held off for the first few bars so a pullback entry "
        "is not sold by the very dip it was bought on."
    )

    def check(
        self,
        position: EnginePosition,
        bars: list[DailyBar],
        price: Decimal,
        params: dict[str, float],
        now: datetime,
    ) -> ExitDecision | None:
        # A pullback entry is, by construction, below its 20-day. Without a grace
        # period this rule sells it on the next scan for a zero-P&L round trip.
        # The stop still runs first, so capital is protected the whole time.
        if position.bars_held < int(params.get("indicator_grace_bars", 3)):
            return None
        value = float(price)
        sma20 = last(indicator("sma", bars, period=20)["value"])
        if sma20 is not None and value < sma20:
            return ExitDecision(
                rule=self.key,
                reason=f"closed below the 20-day ({sma20:.2f}) — short-term trend lost",
            )

        hist_series = indicator("macd", bars, fast=12, slow=26, signal=9)["hist"]
        hist, hist_prev = last(hist_series), prior(hist_series, 1)
        if hist is not None and hist_prev is not None and hist < 0 <= hist_prev:
            return ExitDecision(
                rule=self.key,
                reason=f"MACD histogram flipped negative ({hist:+.2f}) — momentum rolled over",
            )

        rsi_series = indicator("rsi", bars, period=14)["value"]
        rsi, rsi_prev = last(rsi_series), prior(rsi_series, 1)
        hot = params.get("rsi_exit", 80.0)
        if rsi is not None and rsi_prev is not None and rsi_prev >= hot and rsi < rsi_prev:
            return ExitDecision(
                rule=self.key,
                reason=f"RSI peaked at {rsi_prev:.0f} and turned down — blow-off cooling",
            )
        return None


class TimeExit(ExitRule):
    key = "time"
    name = "Time Exit"
    description = (
        "Close a trade that has gone nowhere for N bars. Dead money is still money: "
        "capital parked in a stalled name is capital not in the next setup."
    )

    def check(
        self,
        position: EnginePosition,
        bars: list[DailyBar],
        price: Decimal,
        params: dict[str, float],
        now: datetime,
    ) -> ExitDecision | None:
        max_bars = int(params.get("max_bars", 10))
        if position.bars_held < max_bars:
            return None
        move = position.pnl_pct_at(price)
        drift = f"{move:+.1f}%" if move is not None else "flat"
        return ExitDecision(
            rule=self.key,
            reason=f"held {position.bars_held} bars and only moved {drift} — freeing the capital",
        )


class SessionClose(ExitRule):
    key = "session_close"
    name = "Session Close"
    description = (
        "Flat by the bell. Off unless flat_at_minute is set, because a swing engine "
        "is built to carry positions overnight — this is what turns one into a "
        "day-mode engine, where the whole point is to hold no risk through a gap."
    )

    def check(
        self,
        position: EnginePosition,
        bars: list[DailyBar],
        price: Decimal,
        params: dict[str, float],
        now: datetime,
    ) -> ExitDecision | None:
        flat_at = int(params.get("flat_at_minute", 0))
        if flat_at <= 0:
            return None
        if now.hour * 100 + now.minute < flat_at:
            return None
        move = position.pnl_pct_at(price)
        drift = f"{move:+.1f}%" if move is not None else "flat"
        return ExitDecision(
            rule=self.key,
            reason=(
                f"session close at {flat_at // 100:02d}:{flat_at % 100:02d} — "
                f"out at {drift}, holding nothing through the overnight gap"
            ),
        )


# Order matters: capital protection first, profit-taking second, housekeeping last.
# session_close sits at the end deliberately: if price is already through the stop
# at 15:55 the stop is the truer reason, and the R-multiple should say so.
RULES: list[ExitRule] = [
    StopLoss(),
    TrailingStop(),
    ProfitTarget(),
    IndicatorExit(),
    TimeExit(),
    SessionClose(),
]
REGISTRY: dict[str, ExitRule] = {rule.key: rule for rule in RULES}

# Exit params that are inert without their rule. A stored config can outlive the
# build that understands it — a database configured by a current CLI, read by a
# pod running older code — and the failure is silent: the param is simply never
# consulted, so a day engine quietly behaves like a swing engine and carries the
# overnight risk its configuration exists to forbid. Only meaningful when the
# param is switched on; a `flat_at_minute` of 0 needs no rule.
PARAM_RULES: dict[str, str] = {"flat_at_minute": "session_close"}


def missing_rules(params: dict[str, float]) -> list[tuple[str, str]]:
    """(param, rule) pairs the config asks for that this build cannot provide."""
    return [
        (param, rule)
        for param, rule in PARAM_RULES.items()
        if float(params.get(param, 0)) > 0 and rule not in REGISTRY
    ]


def evaluate(
    position: EnginePosition,
    bars: list[DailyBar],
    price: Decimal,
    params: dict[str, float],
    now: datetime,
) -> ExitDecision | None:
    """First rule to fire wins."""
    for rule in RULES:
        decision = rule.check(position, bars, price, params, now)
        if decision is not None:
            return decision
    return None
