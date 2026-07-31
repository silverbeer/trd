from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar

from pydantic import BaseModel

from trd.indicators import REGISTRY as INDICATORS
from trd.models import Bar


class StrategySignal(BaseModel):
    """What an entry rule emits when its conditions line up.

    `reason` is the point of the whole thing — a plain-English read of *why* the
    rule fired, in the same voice as the indicator panel. A signal you can't
    explain is a signal you can't learn from.
    """

    strategy: str
    score: float  # 0..1 confidence, used only to rank same-bar candidates
    reason: str


class Strategy(ABC):
    """One entry rule in the code registry. Mirrors the Indicator contract:
    the math lives in evaluate(), the teaching read rides along in the signal."""

    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    min_bars: ClassVar[int] = 200

    @abstractmethod
    def evaluate(self, bars: Sequence[Bar]) -> StrategySignal | None:
        """Return a signal if the rule fires on the last bar, else None."""


REGISTRY: dict[str, Strategy] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    REGISTRY[cls.key] = cls()
    return cls


def indicator(key: str, bars: Sequence[Bar], **params: Any) -> dict[str, list[float | None]]:
    """Run a registered indicator over bars. Strategies never reimplement math
    that the indicator registry already owns."""
    return INDICATORS[key].compute(bars, **params)


def last(series: list[float | None]) -> float | None:
    return series[-1] if series else None


def prior(series: list[float | None], back: int = 1) -> float | None:
    """Value `back` bars before the end, or None if it isn't there yet."""
    index = len(series) - 1 - back
    return series[index] if index >= 0 else None


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
