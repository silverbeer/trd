"""What kind of company a symbol is: sector, industry, country, summary (SB-1244).

Adding names from Telegram is the common path, and the reply used to say only
that a ticker resolved. These pin that an add stores the provider's profile,
that adding a name again repairs a profile the first fetch left thin, and that
neither a thin fetch nor a provider outage can damage what is already stored.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.db.connection import connect
from trd.errors import ProviderError
from trd.models import DailyBar, Instrument, InstrumentInfo, InstrumentType
from trd.repos.instruments import InstrumentRepo
from trd.services.backup import export_data, restore_data
from trd.services.commands import CommandQueueService
from trd.services.engine import EngineService

PROFILE = {
    "sector": "Industrials",
    "industry": "Electrical Equipment & Parts",
    "country": "United States",
    "summary": "Vertiv designs power and cooling for data centers.",
}


def _stored(conn: duckdb.DuckDBPyConnection, symbol: str) -> Instrument:
    found = InstrumentRepo(conn).get_by_symbol(symbol)
    assert found is not None
    return found


def _bars(n: int) -> list[DailyBar]:
    start = date.today() - timedelta(days=n - 1)
    return [
        DailyBar(
            date=start + timedelta(days=i),
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
            volume=1_000_000,
        )
        for i in range(n)
    ]


def _profiled(provider: FakeProvider, symbol: str) -> None:
    provider.add_symbol(symbol, price="100", name="Vertiv Holdings Co")
    provider.add_bars(symbol, _bars(300))
    provider.infos[symbol] = provider.infos[symbol].model_copy(update=PROFILE)


@pytest.fixture
def service(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> CommandQueueService:
    provider.add_symbol("AAA", price="100")
    provider.add_bars("AAA", _bars(300))
    EngineService(conn, provider).init(symbols=["AAA"])
    return CommandQueueService(conn, provider)


def test_an_add_stores_the_profile_and_says_what_the_company_is(service, conn, provider):
    _profiled(provider, "VRT")

    message = service.add("VRT")

    stored = _stored(conn, "VRT")
    assert (stored.sector, stored.industry, stored.country, stored.summary) == tuple(
        PROFILE.values()
    )
    assert "VRT (Vertiv Holdings Co · Industrials / Electrical Equipment & Parts)" in message


def test_adding_again_repairs_a_profile_the_first_fetch_left_blank(service, conn, provider):
    """AAA went in with no sector (the fixture's info has none). The next add —
    the thing the user does from Telegram — is the moment to fill it in."""
    assert _stored(conn, "AAA").sector is None
    provider.infos["AAA"] = provider.infos["AAA"].model_copy(update=PROFILE)

    message = service.add("AAA")

    stored = _stored(conn, "AAA")
    assert stored.category == "Industrials / Electrical Equipment & Parts"
    assert stored.summary == PROFILE["summary"]
    assert "already" in message and "Industrials" in message


def test_a_thin_fetch_never_blanks_what_a_good_one_stored(conn, provider):
    repo = InstrumentRepo(conn)
    repo.insert(InstrumentInfo(symbol="VRT", sector="Industrials", industry=None))

    # Yahoo on a bad day: industry arrives, sector does not.
    repo.enrich("VRT", InstrumentInfo(symbol="VRT", sector=None, industry="Electrical Equipment"))

    stored = _stored(conn, "VRT")
    assert stored.sector == "Industrials"
    assert stored.industry == "Electrical Equipment"


def test_enrich_does_not_overwrite_a_stored_value(conn):
    repo = InstrumentRepo(conn)
    repo.insert(InstrumentInfo(symbol="VRT", sector="Industrials"))
    repo.enrich("VRT", InstrumentInfo(symbol="VRT", sector="Technology"))
    assert _stored(conn, "VRT").sector == "Industrials"


def test_enrich_works_on_a_row_other_tables_reference(service, conn, provider):
    """DuckDB rewrites UPDATE ... RETURNING on a referenced row as delete + insert
    and trips the foreign keys. AAA is on the engine's watchlist, so it is
    referenced — this is the shape the live database is in."""
    provider.infos["AAA"] = provider.infos["AAA"].model_copy(update=PROFILE)
    stored = InstrumentRepo(conn).enrich("AAA", provider.infos["AAA"])
    assert stored is not None and stored.industry == PROFILE["industry"]


def test_a_provider_outage_does_not_fail_the_add(service, provider, monkeypatch):
    """Profile is a nice-to-have on an add; the universe change is the point."""

    def down(symbol: str) -> InstrumentInfo:
        raise ProviderError("yahoo is down")

    monkeypatch.setattr(provider, "get_info", down)
    message = service.add("AAA")
    assert "already" in message


def test_a_complete_profile_is_not_refetched(service, conn, provider, monkeypatch):
    _profiled(provider, "VRT")
    service.add("VRT")
    calls: list[str] = []
    real = provider.get_info

    def counting(symbol: str) -> InstrumentInfo:
        calls.append(symbol)
        return real(symbol)

    monkeypatch.setattr(provider, "get_info", counting)
    service.add("VRT")
    assert calls == []


def test_category_is_sector_then_industry_and_none_when_neither():
    assert InstrumentInfo(symbol="X", sector="Technology").category == "Technology"
    assert InstrumentInfo(symbol="X", industry="Semiconductors").category == "Semiconductors"
    assert InstrumentInfo(symbol="X", type=InstrumentType.ETF).category is None
    assert "category" in InstrumentInfo(symbol="X", sector="Energy").model_dump()


def test_backup_round_trips_the_profile(tmp_path: Path, conn, provider):
    InstrumentRepo(conn).insert(InstrumentInfo.model_validate({"symbol": "VRT", **PROFILE}))
    data = export_data(conn)

    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)

    stored = _stored(fresh, "VRT")
    assert stored.industry == PROFILE["industry"]
    assert stored.summary == PROFILE["summary"]


def test_a_backup_from_before_the_profile_still_restores(tmp_path: Path, conn):
    InstrumentRepo(conn).insert(InstrumentInfo(symbol="VRT", sector="Industrials"))
    data = export_data(conn)
    for inst in data["instruments"]:
        for key in ("industry", "country", "summary"):
            inst.pop(key)

    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)

    stored = _stored(fresh, "VRT")
    assert stored.sector == "Industrials"
    assert stored.industry is None
