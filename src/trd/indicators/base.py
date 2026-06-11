from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar

from trd.models import DailyBar


class Category(StrEnum):
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    VOLUME = "volume"


class Indicator(ABC):
    """One indicator in the code registry. The math lives in compute(); the
    plain-English learning-mode read lives in interpret()."""

    key: ClassVar[str]
    name: ClassVar[str]
    category: ClassVar[Category]
    default_params: ClassVar[dict[str, Any]] = {}
    components: ClassVar[list[str]]
    description: ClassVar[str]
    min_bars: ClassVar[int] = 1

    @abstractmethod
    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        """Full series per component, aligned with bars (None during warm-up)."""

    @abstractmethod
    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        """One-line plain-English read of the latest values."""

    def required_bars(self, **params: Any) -> int:
        return self.min_bars


REGISTRY: dict[str, Indicator] = {}


def register(cls: type[Indicator]) -> type[Indicator]:
    REGISTRY[cls.key] = cls()
    return cls


def closes(bars: list[DailyBar]) -> list[float]:
    return [float(b.close) for b in bars]


def latest(series: list[float | None]) -> float | None:
    return series[-1] if series else None
