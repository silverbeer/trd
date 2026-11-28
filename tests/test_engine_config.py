"""`trd engine config` (SB-1824): show and change exit params on a running engine.

The change is the exit-param bag on the existing config row, in place, so the
engine keeps its id, account and universe; the next scan reads the new value,
and an 'open positions' param reaches the trades already on. Every refusal
happens before any write; every change is audited with its reason.
"""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.engine import exits as exit_rules
from trd.engine.exits import DEFAULT_EXIT_PARAMS
from trd.errors import TrdError
from trd.models import EngineMode
from trd.repos.engine import EngineConfigRepo
from trd.services.engine_config import AUDIT_FILENAME, EngineConfigService, ParamScope

from .test_engine import _open_a_trade, engine  # noqa: F401 — fixture

NOW = datetime(2026, 11, 27, 15, 0)
runner = CliRunner()


def _paper(engine, provider):  # noqa: F811
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])


def test_show_lists_every_param_with_its_default_and_scope(engine, provider, conn):  # noqa: F811
    _paper(engine, provider)
    view = EngineConfigService(conn).show()
    keys = [p.key for p in view.params]
    assert keys == list(DEFAULT_EXIT_PARAMS)
    by = {p.key: p for p in view.params}
    assert by["trail_atr_mult"].value == 3.0 and by["trail_atr_mult"].changed is False
    assert by["trail_atr_mult"].scope == ParamScope.OPEN
    assert by["stop_atr_mult"].scope == ParamScope.NEW
    assert by["rotate_min_score"].scope == ParamScope.BACKTEST


def test_set_changes_the_bag_in_place_and_keeps_the_config(engine, provider, conn, tmp_path):  # noqa: F811
    _paper(engine, provider)
    before = EngineConfigRepo(conn).get()
    assert before is not None

    change = EngineConfigService(conn).set(
        {"trail_atr_mult": 1.5}, "protect the wins", NOW, audit_dir=tmp_path
    )

    after = EngineConfigRepo(conn).get()
    assert after is not None
    assert after.id == before.id
    assert after.account_id == before.account_id
    assert after.watchlist == before.watchlist
    assert after.strategies == before.strategies
    assert after.exit_params["trail_atr_mult"] == 1.5
    # every other param untouched
    assert {k: v for k, v in after.exit_params.items() if k != "trail_atr_mult"} == {
        k: v for k, v in before.exit_params.items() if k != "trail_atr_mult"
    }
    assert [(c.key, c.old, c.new) for c in change.changes] == [("trail_atr_mult", 3.0, 1.5)]

    lines = (tmp_path / AUDIT_FILENAME).read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["key"] == "trail_atr_mult" and entry["old"] == 3.0 and entry["new"] == 1.5
    assert entry["reason"] == "protect the wins"


def test_setting_the_same_value_writes_nothing(engine, provider, conn, tmp_path):  # noqa: F811
    _paper(engine, provider)
    change = EngineConfigService(conn).set(
        {"trail_atr_mult": 3.0}, "no-op", NOW, audit_dir=tmp_path
    )
    assert change.changes == []
    assert not (tmp_path / AUDIT_FILENAME).exists()


@pytest.mark.parametrize(
    ("values", "reason", "match"),
    [
        ({"trail_mult": 1.5}, "x", "Unknown exit params: trail_mult"),
        ({"rotate_min_score": 0.5}, "x", "backtest only"),
        ({"max_per_symbol": 2}, "x", "backtest only"),
        ({"trail_atr_mult": 0}, "x", "greater than 0"),
        ({"trail_atr_mult": -1}, "x", ">= 0"),
        ({"trail_atr_mult": float("nan")}, "x", ">= 0"),
        ({"trail_atr_mult": 1.5}, "  ", "--reason is required"),
        ({}, "x", "Nothing to set"),
        ({"flat_at_minute": 1555}, "x", "daily"),
    ],
)
def test_refusals_happen_before_any_write(engine, provider, conn, tmp_path, values, reason, match):  # noqa: F811
    _paper(engine, provider)
    before = EngineConfigRepo(conn).get()
    with pytest.raises(TrdError, match=match):
        EngineConfigService(conn).set(values, reason, NOW, audit_dir=tmp_path)
    assert EngineConfigRepo(conn).get() == before
    assert not (tmp_path / AUDIT_FILENAME).exists()


def test_a_live_engine_refuses_a_flat_at_time(engine, provider, conn):  # noqa: F811
    provider.add_symbol("AAA", price="100")
    engine.init(
        account_name="brokerage",
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("50"),
        capital=Decimal("500"),
        mode=EngineMode.LIVE,
    )
    with pytest.raises(TrdError, match="no flat-at"):
        EngineConfigService(conn).set({"flat_at_minute": 1555}, "x", NOW)
    # ... while the trail itself is fine on a live engine
    change = EngineConfigService(conn).set({"trail_atr_mult": 1.5}, "protect wins", NOW)
    assert change.changes[0].new == 1.5


def test_the_new_trail_reaches_a_position_already_open(engine, provider, conn):  # noqa: F811
    """The point of the change: an open trade's trailing stop moves up on the next
    pass, because the rule reads the config's multiplier, not one frozen at entry."""
    _paper(engine, provider)
    _open_a_trade(engine, conn)
    pos = engine.positions.list_open(engine.account().id)[0][0]
    engine.positions.touch(pos.id, Decimal("130"), 3, date(2026, 8, 4))  # peaked at 130, ATR 5

    change = EngineConfigService(conn).set({"trail_atr_mult": 1.5}, "tighter", NOW)
    assert change.open_positions == ["AAA"]

    # explain() reads the live config: 130 - 1.5 x 5 = 122.5 (it was 130 - 3 x 5 = 115)
    trail = {e.rule: e for e in engine.explain("AAA").exits}["trail"]
    assert trail.level == Decimal("122.5")

    # and the exit rule fires at a price the old 3x trail would have held through
    pos = engine.positions.list_open(engine.account().id)[0][0]
    config = EngineConfigRepo(conn).get()
    assert config is not None
    params = {**DEFAULT_EXIT_PARAMS, **config.exit_params}
    hit = exit_rules.TrailingStop().check(pos, [], [], Decimal("120"), params, NOW, "1d")
    assert hit is not None and hit.level == Decimal("122.5")
    old = {**params, "trail_atr_mult": 3.0}
    assert exit_rules.TrailingStop().check(pos, [], [], Decimal("120"), old, NOW, "1d") is None


def test_cli_show_and_set(cli_env, tmp_path):
    cli_env.add_symbol("AAA", price="100")
    assert runner.invoke(app, ["engine", "init", "--symbols", "AAA"]).exit_code == 0

    res = runner.invoke(
        app,
        ["engine", "config", "set", "-p", "trail_atr_mult=1.5", "--reason", "protect", "--json"],
    )
    assert res.exit_code == 0, res.output
    body = json.loads(res.output)
    assert body["changes"][0]["new"] == 1.5

    shown = json.loads(runner.invoke(app, ["engine", "config", "--json"]).output)
    trail = next(p for p in shown["params"] if p["key"] == "trail_atr_mult")
    assert trail["value"] == 1.5 and trail["changed"] is True

    bad = runner.invoke(app, ["engine", "config", "set", "-p", "nope=1", "--reason", "x", "--json"])
    assert bad.exit_code != 0
    assert json.loads(bad.output)["error"]


def test_cli_set_requires_a_reason(cli_env):
    cli_env.add_symbol("AAA", price="100")
    assert runner.invoke(app, ["engine", "init", "--symbols", "AAA"]).exit_code == 0
    res = runner.invoke(app, ["engine", "config", "set", "-p", "trail_atr_mult=1.5"])
    assert res.exit_code != 0


def test_the_positions_view_shows_the_configured_trail(engine, provider, conn):  # noqa: F811
    """SB-1826: the stop a view shows is the stop TrailingStop fires at. It was
    hardcoded at 3 x ATR, so a live engine set to 1.5 sold at 122.5 while
    'engine positions' and 'live-report' still printed 115."""
    _paper(engine, provider)
    _open_a_trade(engine, conn)
    pos = engine.positions.list_open(engine.account().id)[0][0]
    engine.positions.touch(pos.id, Decimal("130"), 3, date(2026, 8, 4))

    assert engine.position_rows()[0].stop_in_force == Decimal("115")  # 130 - 3 x 5
    EngineConfigService(conn).set({"trail_atr_mult": 1.5}, "tighter", NOW)
    row = engine.position_rows()[0]
    assert row.stop_in_force == Decimal("122.5")  # 130 - 1.5 x 5
    # mark 100 sits under the new stop: nothing protected below it, so risk is floored at 0
    assert row.risk_at_stop == Decimal(0)


def test_the_view_keeps_the_initial_stop_until_the_trail_activates(engine, provider, conn):  # noqa: F811
    _paper(engine, provider)
    _open_a_trade(engine, conn)  # entry 100, stop 90, 1R = 10
    pos = engine.positions.list_open(engine.account().id)[0][0]
    engine.positions.touch(pos.id, Decimal("115"), 3, date(2026, 8, 4))
    EngineConfigService(conn).set({"trail_atr_mult": 1.0, "trail_activate_r": 2}, "x", NOW)
    # peaked at +1.5R, activation needs +2R: TrailingStop does not fire, the view agrees
    assert engine.position_rows()[0].stop_in_force == Decimal("90")
    engine.positions.touch(pos.id, Decimal("125"), 4, date(2026, 8, 5))
    assert engine.position_rows()[0].stop_in_force == Decimal("120")  # 125 - 1 x 5
