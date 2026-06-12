from pathlib import Path

import pytest
from typer.testing import CliRunner

import trd.cli.app as cli
from tests.conftest import FakeProvider
from trd.cli.app import app

runner = CliRunner()


@pytest.fixture
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: FakeProvider
) -> FakeProvider:
    monkeypatch.setenv("TRD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "YFinanceProvider", lambda: provider)
    return provider


def test_init_creates_db_and_account(cli_env: FakeProvider, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "main" in result.output
    assert (tmp_path / "home" / "trd.duckdb").exists()


def test_buy_then_portfolio(cli_env: FakeProvider) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["buy", "AAPL", "10", "--price", "150", "--date", "2026-01-05"])
    assert result.exit_code == 0, result.output
    assert "Bought 10 AAPL" in result.output

    result = runner.invoke(app, ["portfolio"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "200.00" in result.output  # live fake quote


def test_sell_more_than_held_fails(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["buy", "AAPL", "5", "--price", "150"])
    result = runner.invoke(app, ["sell", "AAPL", "10", "--price", "180"])
    assert result.exit_code == 1
    assert "only 5 held" in result.output


def test_quote_command(cli_env: FakeProvider) -> None:
    result = runner.invoke(app, ["quote", "btc-usd"])
    assert result.exit_code == 0, result.output
    assert "BTC-USD" in result.output
    assert "crypto" in result.output


def test_quote_unknown_symbol(cli_env: FakeProvider) -> None:
    result = runner.invoke(app, ["quote", "ZZZZZZ"])
    assert result.exit_code == 1


def test_import_command(cli_env: FakeProvider, tmp_path: Path) -> None:
    runner.invoke(app, ["init"])
    csv = tmp_path / "txns.csv"
    csv.write_text("date,account,symbol,side,quantity,price\n2026-01-05,main,NVDA,buy,3,100.00\n")
    result = runner.invoke(app, ["import", str(csv)])
    assert result.exit_code == 0, result.output
    assert "Imported 1" in result.output


def test_invalid_quantity_rejected(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["buy", "AAPL", "ten", "--price", "150"])
    assert result.exit_code == 1
    assert "invalid quantity" in result.output


def test_watch_add_ls_rm(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["watch", "add", "NVDA", "--list", "ai"])
    assert result.exit_code == 0, result.output
    assert "Watching NVDA" in result.output

    result = runner.invoke(app, ["watch", "ls", "ai"])
    assert result.exit_code == 0, result.output
    assert "NVDA" in result.output
    assert "120.00" in result.output

    result = runner.invoke(app, ["watch", "rm", "NVDA", "--list", "ai"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["watch", "ls"])
    assert "Nothing watched" in result.output


def test_watch_rm_unknown_fails(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["watch", "rm", "NVDA"])
    assert result.exit_code == 1


def test_earnings_empty(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["earnings"])
    assert result.exit_code == 0, result.output
    assert "No earnings" in result.output


def test_account_add_and_ls(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["account", "add", "fidelity"])
    assert result.exit_code == 0, result.output
    assert "fidelity" in result.output

    result = runner.invoke(app, ["account", "add", "fidelity"])
    assert result.exit_code == 0
    assert "already exists" in result.output

    result = runner.invoke(app, ["account", "add", "paper", "--type", "simulation"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["account", "ls"])
    assert result.exit_code == 0, result.output
    for name in ("main", "fidelity", "paper"):
        assert name in result.output

    result = runner.invoke(app, ["buy", "AAPL", "1", "--price", "100", "--account", "fidelity"])
    assert result.exit_code == 0, result.output


def test_account_add_bad_type(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["account", "add", "x", "--type", "margin"])
    assert result.exit_code == 1


def test_indicator_lifecycle(cli_env: FakeProvider) -> None:
    result = runner.invoke(app, ["init"])
    assert "Seeded" in result.output

    result = runner.invoke(app, ["indicator", "ls"])
    assert result.exit_code == 0, result.output
    assert "rsi" in result.output and "macd" in result.output

    result = runner.invoke(app, ["indicator", "catalog"])
    assert result.exit_code == 0
    assert "Bollinger" in result.output

    result = runner.invoke(
        app, ["indicator", "add", "ema", "--param", "period=8", "--note", "fast"]
    )
    assert result.exit_code == 0, result.output
    assert "ema" in result.output

    result = runner.invoke(app, ["indicator", "rm", "atr"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["indicator", "info", "rsi"])
    assert result.exit_code == 0
    assert "overbought" in result.output.lower() or "Momentum" in result.output


def test_indicator_add_unknown_fails(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["indicator", "add", "vibes"])
    assert result.exit_code == 1


def test_indicators_panel(cli_env: FakeProvider) -> None:
    from datetime import date, timedelta
    from decimal import Decimal

    from trd.config import get_settings
    from trd.db.connection import connect
    from trd.models import DailyBar
    from trd.repos import InstrumentRepo, PriceRepo

    runner.invoke(app, ["init"])
    runner.invoke(app, ["watch", "add", "AAPL"])

    conn = connect(get_settings().db_path)
    instrument = InstrumentRepo(conn).get_by_symbol("AAPL")
    assert instrument is not None
    today = date.today()
    bars = [
        DailyBar(
            date=today - timedelta(days=300 - i),
            open=Decimal(100 + i),
            high=Decimal(102 + i),
            low=Decimal(99 + i),
            close=Decimal(101 + i),
            volume=1_000_000,
        )
        for i in range(300)
    ]
    PriceRepo(conn).upsert_daily(instrument.id, bars)
    conn.close()

    result = runner.invoke(app, ["indicators", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "TREND" in result.output
    assert "overbought" in result.output  # rising series

    result = runner.invoke(app, ["indicators", "ZZZZ"])
    assert result.exit_code == 1


def test_sim_lifecycle(cli_env: FakeProvider) -> None:
    from trd.models import InstrumentType

    cli_env.add_symbol("SPY", price="500.00", prev_close="495.00", type_=InstrumentType.ETF)
    runner.invoke(app, ["init"])

    result = runner.invoke(app, ["sim", "init", "--monthly", "100"])
    assert result.exit_code == 0, result.output
    assert "100.00/month" in result.output

    result = runner.invoke(app, ["sim", "invest"])
    assert result.exit_code == 0, result.output
    assert "Recorded 0.2 SPY" in result.output

    result = runner.invoke(app, ["sim", "invest"])
    assert result.exit_code == 1  # same month

    result = runner.invoke(app, ["sim", "status"])
    assert result.exit_code == 0, result.output
    assert "Total invested" in result.output
    assert "100.00" in result.output


def test_sim_status_without_init(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["sim", "status"])
    assert result.exit_code == 1


def test_plan_on_real_account_lifecycle(cli_env: FakeProvider) -> None:
    from trd.models import InstrumentType

    cli_env.add_symbol("SPY", price="500.00", prev_close="495.00", type_=InstrumentType.ETF)
    cli_env.add_symbol("QQQ", price="200.00", type_=InstrumentType.ETF)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["account", "add", "sofi"])

    result = runner.invoke(
        app,
        [
            "plan",
            "set",
            "--account",
            "sofi",
            "--monthly",
            "100",
            "--alloc",
            "SPY=30",
            "--alloc",
            "QQQ=70",
            "--note",
            "real monthly DCA",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "real money" in result.output

    result = runner.invoke(app, ["plan", "invest"])  # single plan: --account optional
    assert result.exit_code == 0, result.output
    assert "Recorded" in result.output
    assert "broker" in result.output  # real-account reminder

    result = runner.invoke(app, ["plan", "status", "--account", "sofi"])
    assert result.exit_code == 0, result.output
    assert "Total invested" in result.output

    result = runner.invoke(app, ["plan", "ls"])
    assert result.exit_code == 0, result.output
    assert "sofi" in result.output and "real" in result.output
    assert "DCA" in result.output  # goal column


def test_plan_set_unknown_account_fails(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["plan", "set", "--account", "nope"])
    assert result.exit_code == 1


def test_earnings_after_sync(cli_env: FakeProvider) -> None:
    from datetime import date, timedelta

    from trd.models import EarningsDate

    runner.invoke(app, ["init"])
    runner.invoke(app, ["watch", "add", "AAPL"])
    cli_env.earnings["AAPL"] = [EarningsDate(date=date.today() + timedelta(days=4))]
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "1 earnings dates" in result.output

    result = runner.invoke(app, ["earnings", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "4d" in result.output
