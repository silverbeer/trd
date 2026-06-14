"""Bundled reference data that ships with trd (no network, no DB).

These are static lists trd reasons over but can't derive from the user's own
holdings: the index/futures symbols to snapshot, a curated large-cap universe to
scan for earnings, and the recurring macro calendar (FOMC meetings, jobs/CPI/PPI
cadence). Edit these files to broaden coverage; they are plain Python so the type
checker keeps them honest.
"""

from trd.data.market_calendar import EconEvent, events_for_week
from trd.data.universe import (
    FUTURES,
    INDEX_PROXIES,
    SECTOR_ETFS,
    UNIVERSE,
    VIX_SYMBOL,
    name_for,
)

__all__ = [
    "FUTURES",
    "INDEX_PROXIES",
    "SECTOR_ETFS",
    "UNIVERSE",
    "VIX_SYMBOL",
    "EconEvent",
    "events_for_week",
    "name_for",
]
