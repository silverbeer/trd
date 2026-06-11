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
