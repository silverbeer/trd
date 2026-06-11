from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from trd.errors import TrdError
from trd.models import DailyBar, InstrumentInfo, InstrumentType
from trd.repos import InstrumentRepo, PriceRepo
from trd.services import IndicatorService
from trd.services.indicators import DEFAULT_CONFIGS, seed_defaults


def _track_with_bars(conn: duckdb.DuckDBPyConnection, symbol: str, days: int) -> None:
    instrument = InstrumentRepo(conn).insert(
        InstrumentInfo(symbol=symbol, name=f"{symbol} Inc", type=InstrumentType.STOCK)
    )
    today = date.today()
    bars = [
        DailyBar(
            date=today - timedelta(days=days - i),
            open=Decimal(100 + i),
            high=Decimal(102 + i),
            low=Decimal(99 + i),
            close=Decimal(101 + i),
            volume=1_000_000 + (i % 7) * 50_000,
        )
        for i in range(days)
    ]
    PriceRepo(conn).upsert_daily(instrument.id, bars)


@pytest.fixture
def service(conn: duckdb.DuckDBPyConnection) -> IndicatorService:
    seed_defaults(conn)
    return IndicatorService(conn)


def test_seed_defaults_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    assert seed_defaults(conn) == len(DEFAULT_CONFIGS)
    assert seed_defaults(conn) == 0


def test_panel_computes_all_defaults(service: IndicatorService) -> None:
    _track_with_bars(service.conn, "AAPL", 300)
    rows = service.panel("aapl")
    assert len(rows) == len(DEFAULT_CONFIGS)
    by_key = {(r.config.key, str(r.config.params.get("period", ""))): r for r in rows}
    rsi_row = by_key[("rsi", "14")]
    assert rsi_row.values["value"] == 100.0  # strictly rising closes
    assert "overbought" in rsi_row.reading
    sma200 = by_key[("sma", "200")]
    assert sma200.values["value"] is not None
    assert "above" in sma200.reading  # rising series sits above its average


def test_panel_insufficient_bars_explains(service: IndicatorService) -> None:
    _track_with_bars(service.conn, "NEWIPO", 30)
    rows = service.panel("NEWIPO")
    sma200 = next(r for r in rows if r.config.key == "sma" and r.config.params["period"] == 200)
    assert "needs 200 bars" in sma200.reading
    rsi_row = next(r for r in rows if r.config.key == "rsi")
    assert rsi_row.values  # 30 bars is enough for rsi(14)


def test_panel_untracked_symbol_raises(service: IndicatorService) -> None:
    with pytest.raises(TrdError, match="not tracked"):
        service.panel("ZZZZ")


def test_panel_no_bars_raises(service: IndicatorService) -> None:
    InstrumentRepo(service.conn).insert(InstrumentInfo(symbol="EMPTY", type=InstrumentType.STOCK))
    with pytest.raises(TrdError, match="No price history"):
        service.panel("EMPTY")


def test_add_unknown_key_rejected(service: IndicatorService) -> None:
    with pytest.raises(TrdError, match="No indicator 'vibes'"):
        service.add("vibes", {})


def test_add_unknown_param_rejected(service: IndicatorService) -> None:
    with pytest.raises(TrdError, match="Unknown params"):
        service.add("rsi", {"window": 14})


def test_add_same_key_different_params(service: IndicatorService) -> None:
    service.add("ema", {"period": 8}, note="fast line")
    service.add("ema", {"period": 21})
    enabled = service.configs.list_enabled()
    assert len([c for c in enabled if c.key == "ema"]) == 2


def test_remove_soft_disables_keeps_history(service: IndicatorService) -> None:
    assert service.remove("macd") == 1
    assert all(c.key != "macd" for c in service.configs.list_enabled())
    disabled = [c for c in service.configs.list_all() if c.key == "macd"]
    assert len(disabled) == 1
    assert disabled[0].enabled is False


def test_remove_param_match(service: IndicatorService) -> None:
    assert service.remove("sma", {"period": 200}) == 1
    remaining = [c.params["period"] for c in service.configs.list_enabled() if c.key == "sma"]
    assert sorted(remaining) == [20, 50]


def test_remove_nothing_raises(service: IndicatorService) -> None:
    with pytest.raises(TrdError, match="No enabled indicator"):
        service.remove("vibes")


def test_unknown_config_key_auto_disabled(service: IndicatorService) -> None:
    service.configs.add("ghost", {"period": 1})
    warnings = service.validate_configs()
    assert any("ghost" in w for w in warnings)
    assert all(c.key != "ghost" for c in service.configs.list_enabled())
    # disabled row records the reason in its note
    ghost = next(c for c in service.configs.list_all() if c.key == "ghost")
    assert "auto-disabled" in (ghost.note or "")
