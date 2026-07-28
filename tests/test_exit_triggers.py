from decimal import Decimal

import duckdb
import pytest

from trd.errors import TrdError
from trd.models import AccountType, ExitStatus
from trd.repos import AccountRepo
from trd.services.exit_triggers import ExitTriggerService

from .conftest import FakeProvider, seed_bars


@pytest.fixture
def exits(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> ExitTriggerService:
    AccountRepo(conn).create("rh-agent", AccountType.SIMULATION)
    return ExitTriggerService(conn, provider)


def test_set_creates_trigger(exits: ExitTriggerService) -> None:
    trigger = exits.set("AAPL", "rh-agent", stop=Decimal("180"), target=Decimal("220"))
    assert trigger.stop_price == Decimal("180")
    assert trigger.target_price == Decimal("220")
    rows = exits.rows("rh-agent")
    assert len(rows) == 1
    assert rows[0].instrument.symbol == "AAPL"


def test_set_is_upsert(exits: ExitTriggerService) -> None:
    exits.set("AAPL", "rh-agent", stop=Decimal("180"))
    exits.set("AAPL", "rh-agent", stop=Decimal("190"), note="tightened")
    rows = exits.rows("rh-agent")
    assert len(rows) == 1  # replaced, not duplicated
    assert rows[0].stop_price == Decimal("190")
    assert rows[0].note == "tightened"


def test_set_requires_a_level(exits: ExitTriggerService) -> None:
    with pytest.raises(TrdError, match="at least one"):
        exits.set("AAPL", "rh-agent")


def test_stop_must_be_below_target(exits: ExitTriggerService) -> None:
    with pytest.raises(TrdError, match="below target"):
        exits.set("AAPL", "rh-agent", stop=Decimal("220"), target=Decimal("200"))


def test_unknown_account_fails(exits: ExitTriggerService) -> None:
    with pytest.raises(TrdError, match="No account"):
        exits.set("AAPL", "ghost", stop=Decimal("100"))


def test_remove(exits: ExitTriggerService) -> None:
    exits.set("AAPL", "rh-agent", stop=Decimal("180"))
    exits.remove("AAPL", "rh-agent")
    assert exits.rows("rh-agent") == []
    with pytest.raises(TrdError, match="No exit trigger"):
        exits.remove("AAPL", "rh-agent")


def test_status_no_price_without_bars(exits: ExitTriggerService) -> None:
    exits.set("AAPL", "rh-agent", stop=Decimal("180"))
    assert exits.rows("rh-agent")[0].status == ExitStatus.NO_PRICE


def test_status_holding_stop_and_target(
    exits: ExitTriggerService, conn: duckdb.DuckDBPyConnection
) -> None:
    # 10 flat bars at ~100 → latest close 100, between stop 90 and target 120.
    seed_bars(conn, "AAPL", days=10, start_price=100.0, daily_gain=0.0)
    exits.set("AAPL", "rh-agent", stop=Decimal("90"), target=Decimal("120"))
    row = exits.rows("rh-agent")[0]
    assert row.status == ExitStatus.OK
    assert row.last_close == Decimal("100.0000")
    assert row.stop_cushion_pct == Decimal("10")  # (100-90)/100
    assert row.target_upside_pct == Decimal("20")  # (120-100)/100


def test_status_stop_hit(exits: ExitTriggerService, conn: duckdb.DuckDBPyConnection) -> None:
    seed_bars(conn, "AAPL", days=10, start_price=100.0, daily_gain=0.0)
    exits.set("AAPL", "rh-agent", stop=Decimal("105"))  # close 100 ≤ stop 105
    assert exits.rows("rh-agent")[0].status == ExitStatus.STOP_HIT


def test_status_target_hit(exits: ExitTriggerService, conn: duckdb.DuckDBPyConnection) -> None:
    seed_bars(conn, "AAPL", days=10, start_price=100.0, daily_gain=0.0)
    exits.set("AAPL", "rh-agent", target=Decimal("95"))  # close 100 ≥ target 95
    assert exits.rows("rh-agent")[0].status == ExitStatus.TARGET_HIT


def test_check_filters_to_breaches(
    exits: ExitTriggerService, conn: duckdb.DuckDBPyConnection
) -> None:
    seed_bars(conn, "AAPL", days=10, start_price=100.0, daily_gain=0.0)
    seed_bars(conn, "NVDA", days=10, start_price=100.0, daily_gain=0.0)
    exits.set("AAPL", "rh-agent", stop=Decimal("90"))  # holding (close 100 > 90)
    exits.set("NVDA", "rh-agent", stop=Decimal("105"))  # breached (close 100 ≤ 105)
    breaches = exits.rows("rh-agent", breaches_only=True)
    assert [r.instrument.symbol for r in breaches] == ["NVDA"]
    assert exits.rows("rh-agent", breaches_only=True)[0].status == ExitStatus.STOP_HIT


def test_rows_scoped_by_account(exits: ExitTriggerService, conn: duckdb.DuckDBPyConnection) -> None:
    AccountRepo(conn).create("other", AccountType.SIMULATION)
    exits.set("AAPL", "rh-agent", stop=Decimal("90"))
    exits.set("NVDA", "other", stop=Decimal("90"))
    assert len(exits.rows("rh-agent")) == 1
    assert len(exits.rows()) == 2  # all accounts
