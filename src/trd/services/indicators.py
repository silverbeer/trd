import duckdb
from pydantic import BaseModel

from trd.errors import TrdError
from trd.indicators import REGISTRY, Indicator
from trd.models import DailyBar, IndicatorConfig
from trd.repos import InstrumentRepo
from trd.repos.indicator_config import IndicatorConfigRepo

# Seeded into indicator_config on `trd init` (DESIGN.md "Key Indicators" start set).
DEFAULT_CONFIGS: list[tuple[str, dict]] = [
    ("sma", {"period": 20}),
    ("sma", {"period": 50}),
    ("sma", {"period": 200}),
    ("rsi", {"period": 14}),
    ("macd", {"fast": 12, "slow": 26, "signal": 9}),
    ("bollinger", {"period": 20, "mult": 2.0}),
    ("atr", {"period": 14}),
    ("range52w", {}),
    ("volratio", {"period": 20}),
]


class PanelRow(BaseModel):
    config: IndicatorConfig
    name: str
    category: str
    values: dict[str, float | None]
    reading: str


def seed_defaults(conn: duckdb.DuckDBPyConnection) -> int:
    """Populate the followed list on first run. No-op if any config exists."""
    repo = IndicatorConfigRepo(conn)
    if repo.count() > 0:
        return 0
    for key, params in DEFAULT_CONFIGS:
        repo.add(key, params, note="seeded default")
    return len(DEFAULT_CONFIGS)


class IndicatorService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.configs = IndicatorConfigRepo(conn)
        self.instruments = InstrumentRepo(conn)

    def validate_configs(self) -> list[str]:
        """Auto-disable config rows whose key is missing from the code registry.
        Config can never break the app. Returns warnings."""
        warnings = []
        for config in self.configs.list_enabled():
            if config.key not in REGISTRY:
                self.configs.disable(config.id, reason=f"auto-disabled: unknown key '{config.key}'")
                warnings.append(
                    f"indicator config #{config.id} key '{config.key}' not in registry — disabled"
                )
        return warnings

    def add(self, key: str, params: dict, note: str | None = None) -> IndicatorConfig:
        indicator = REGISTRY.get(key)
        if indicator is None:
            known = ", ".join(sorted(REGISTRY))
            raise TrdError(f"No indicator '{key}' in the registry. Available: {known}")
        merged = {**indicator.default_params, **params}
        unknown = set(merged) - set(indicator.default_params)
        if unknown:
            raise TrdError(f"Unknown params for '{key}': {sorted(unknown)}")
        return self.configs.add(key, merged, note=note)

    def remove(self, key: str, params: dict | None = None) -> int:
        """Soft-disable all enabled configs for a key (optionally param-matched)."""
        matches = [
            c
            for c in self.configs.list_enabled()
            if c.key == key
            and (params is None or all(c.params.get(k) == v for k, v in params.items()))
        ]
        if not matches:
            raise TrdError(f"No enabled indicator '{key}' to remove.")
        for config in matches:
            self.configs.disable(config.id, reason="removed by user")
        return len(matches)

    def catalog(self) -> list[Indicator]:
        return sorted(REGISTRY.values(), key=lambda i: (i.category, i.key))

    def _bars(self, symbol: str) -> list[DailyBar]:
        instrument = self.instruments.get_by_symbol(symbol)
        if instrument is None:
            raise TrdError(f"{symbol.upper()} is not tracked. Buy or watch it first, then sync.")
        rows = self.conn.execute(
            """
            SELECT date, open, high, low, close, volume FROM price_daily
            WHERE instrument_id = ? ORDER BY date
            """,
            [instrument.id],
        ).fetchall()
        return [
            DailyBar(date=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
            for r in rows
        ]

    def panel(self, symbol: str) -> list[PanelRow]:
        """Every enabled indicator evaluated against a symbol's daily bars."""
        self.validate_configs()
        bars = self._bars(symbol)
        if not bars:
            raise TrdError(f"No price history for {symbol.upper()}. Run 'trd sync --full' first.")
        rows: list[PanelRow] = []
        for config in self.configs.list_enabled():
            indicator = REGISTRY[config.key]
            needed = indicator.required_bars(**config.params)
            if len(bars) < needed:
                rows.append(
                    PanelRow(
                        config=config,
                        name=indicator.name,
                        category=indicator.category.value,
                        values={},
                        reading=f"needs {needed} bars, have {len(bars)} — run 'trd sync --full'",
                    )
                )
                continue
            series = indicator.compute(bars, **config.params)
            rows.append(
                PanelRow(
                    config=config,
                    name=indicator.name,
                    category=indicator.category.value,
                    values={component: s[-1] for component, s in series.items()},
                    reading=indicator.interpret(series, bars),
                )
            )
        category_order = ["trend", "momentum", "volatility", "volume"]
        rows.sort(
            key=lambda r: (
                category_order.index(r.category) if r.category in category_order else 99,
                r.config.display_order or 0,
            )
        )
        return rows
