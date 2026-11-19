"""Human slots on the live engine (SB-1742): the owner picks the entry, the engine
runs the exit.

A human slot is an engine_position with strategy 'human', opened from a buy
intent the owner asked for. One capital, two sleeves: the human sleeve's capital
is added to the total and replayed on its own T+1 ledger with its own bridge, so
neither sleeve can spend the other's cash. The engine's slots, caps, halt and
loss stop are the engine's; the human sleeve has its own slot count and loss
stop. "Let it run" skips the profit target and the time exit. The clock is
passed in; FakeProvider supplies quotes.
"""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.db.connection import connect
from trd.errors import TrdError
from trd.models import HUMAN_STRATEGY, IntentStatus, Side
from trd.repos import InstrumentRepo, OrderIntentRepo, TransactionRepo
from trd.repos.engine import EnginePositionRepo
from trd.services import EngineService, FillIngestService, OutboxService
from trd.services.backup import export_data, restore_data
from trd.services.engine import strategy_stats
from trd.services.human import HumanService

from .conftest import FakeProvider
from .test_engine_fills import _order, _snap
from .test_engine_live import CAPITAL, MONDAY, _filled_position, _live, _names
from .test_engine_outbox import _no_telegram

__all__ = ["_no_telegram"]  # autouse: no inherited Telegram config

D = Decimal
runner = CliRunner()
FILLED = MONDAY + timedelta(minutes=31)  # FILLED_AT_UTC in ET
HUMAN_CAPITAL = D("10000")


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


@pytest.fixture
def human(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> HumanService:
    return HumanService(conn, provider)


def _sleeve(engine, human, conn, provider, *symbols: str, slots: int = 2, **live):
    """A live engine on `symbols` with a human sleeve of `slots` x 5,000."""
    _names(conn, provider, *symbols)
    _live(engine, list(symbols), **live)
    human.set(MONDAY, slots=slots, capital=HUMAN_CAPITAL)


def _bought(engine, human, conn, symbol: str, price: str = "100"):
    """A human buy, placed and filled at `price`."""
    intent = human.buy(symbol, MONDAY).intent.intent
    FillIngestService(conn).ingest(_snap([_order(intent, symbol, price=price)]), FILLED)
    pairs = [
        p for p, i in EnginePositionRepo(conn).list_open(engine.account().id) if i.symbol == symbol
    ]
    assert len(pairs) == 1
    return intent, pairs[0]


# --------------------------------------------------------------------- the sleeve


def test_set_adds_the_human_capital_on_top_and_leaves_the_engine_untouched(
    engine, human, conn, provider
):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    before = engine.cash(MONDAY.date())
    assert before is not None

    view = human.set(MONDAY, slots=2, capital=HUMAN_CAPITAL)

    assert engine.config().capital == CAPITAL + HUMAN_CAPITAL
    assert view.sleeve is not None and view.sleeve.size == D("5000")
    assert view.engine_capital == CAPITAL
    # Default loss stop: 20% of the human capital.
    assert view.sleeve.max_loss == D("2000")
    after = engine.cash(MONDAY.date())
    assert after is not None and after.buying_power == before.buying_power
    human_cash = engine.human_cash(MONDAY.date())
    assert human_cash is not None and human_cash.buying_power == HUMAN_CAPITAL

    # Changing the human capital moves the total by the difference only.
    human.set(MONDAY, capital=D("12000"))
    assert engine.config().capital == CAPITAL + D("12000")
    human.set(MONDAY, slots=3)
    assert engine.config().capital == CAPITAL + D("12000")


def test_set_needs_slots_and_capital_the_first_time_and_is_live_only(engine, human, conn, provider):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    with pytest.raises(TrdError, match="both --slots and --capital"):
        human.set(MONDAY, slots=2)

    paper = EngineService(conn, provider)
    paper.init(symbols=["AAA"], strategies=["momentum"], account_name="paper")
    with pytest.raises(TrdError, match="live engine"):
        HumanService(conn, provider).set(MONDAY, slots=2, capital=HUMAN_CAPITAL)


def test_the_human_bridge_is_its_own(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    human.set(MONDAY, bridge=D("500"))
    human_cash = engine.human_cash(MONDAY.date())
    engine_cash = engine.cash(MONDAY.date())
    assert human_cash is not None and human_cash.bridge == D("500")
    assert engine_cash is not None and engine_cash.bridge == D(0)
    human.set(MONDAY, no_bridge=True)
    human_cash = engine.human_cash(MONDAY.date())
    assert human_cash is not None and human_cash.bridge == D(0)


# ---------------------------------------------------------------------- the buy


def test_a_human_buy_is_an_intent_sized_to_one_slot_with_the_engines_plan(
    engine, human, conn, provider
):
    _sleeve(engine, human, conn, provider, "AAA")

    result = human.buy("aaa", MONDAY, note="earnings beat")

    intent = result.intent.intent
    assert intent.side == Side.BUY and intent.strategy == HUMAN_STRATEGY
    assert intent.status == IntentStatus.PENDING
    assert intent.notional <= D("5000") and intent.notional > D("4990")
    assert intent.stop_price is not None and intent.stop_price < intent.ref_price
    assert intent.target_price is not None and intent.target_price > intent.ref_price
    assert intent.atr_at_entry is not None
    assert "earnings beat" in intent.reason
    # Reserved against the human sleeve only.
    human_cash = engine.human_cash(MONDAY.date())
    assert human_cash is not None
    assert human_cash.buying_power == HUMAN_CAPITAL - intent.notional
    engine_cash = engine.cash(MONDAY.date())
    assert engine_cash is not None and engine_cash.buying_power == CAPITAL
    # The runner gets it from the outbox like any other order.
    outbox = OutboxService(conn).pending(MONDAY)
    assert [o.symbol for o in outbox.orders] == ["AAA"]


def test_the_fill_opens_a_human_position_the_engine_manages(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")

    intent, position = _bought(engine, human, conn, "AAA", price="100")

    assert position.strategy == HUMAN_STRATEGY and position.entry_price == D("100")
    # Stop and target re-derived from the fill, as for an engine entry.
    assert position.stop_price < D("100") < position.target_price
    # The human sleeve paid for it; the engine's cash never moved.
    cost = intent.quantity * D("100")
    human_cash = engine.human_cash(FILLED.date())
    engine_cash = engine.cash(FILLED.date())
    account = engine.account_cash(FILLED.date())
    assert human_cash is not None and human_cash.settled == HUMAN_CAPITAL - cost
    assert engine_cash is not None and engine_cash.settled == CAPITAL
    assert account is not None and account.settled == CAPITAL + HUMAN_CAPITAL - cost


def test_a_human_buy_is_refused_outside_the_session(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    for when in (
        MONDAY.replace(hour=9, minute=0),  # pre-market
        MONDAY.replace(hour=16, minute=0),  # the close
        MONDAY - timedelta(days=1),  # Sunday
    ):
        with pytest.raises(TrdError, match="during the session"):
            human.buy("AAA", when)


def test_a_human_buy_is_refused_when_the_slots_are_full(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA", "BBB", "CCC")
    human.buy("AAA", MONDAY)
    _bought(engine, human, conn, "BBB")
    with pytest.raises(TrdError, match="All 2 human slot"):
        human.buy("CCC", MONDAY)
    with pytest.raises(TrdError, match="cannot go below"):
        human.set(MONDAY, slots=1)


def test_a_name_has_one_owner(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA", "BBB")
    _filled_position(engine, conn, "AAA")  # the engine holds AAA
    with pytest.raises(TrdError, match="held by the engine"):
        human.buy("AAA", MONDAY)
    _bought(engine, human, conn, "BBB")
    with pytest.raises(TrdError, match="held by a human slot"):
        human.buy("BBB", MONDAY)


def test_a_human_buy_is_refused_when_human_cash_is_short(engine, human, conn, provider):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    human.set(MONDAY, slots=1, capital=D("5000"), max_loss=D("4000"))
    # Spend most of the human sleeve through a filled human buy, then sell it at a
    # loss: the proceeds are unsettled until T+1 and there is no human bridge.
    intent, position = _bought(engine, human, conn, "AAA", price="100")
    sell = OrderIntentRepo(conn).insert(
        account_id=engine.account().id,
        instrument_id=position.instrument_id,
        side=Side.SELL,
        quantity=intent.quantity,
        ref_price=D("90"),
        reason="stopped",
        strategy=HUMAN_STRATEGY,
        created_at=FILLED,
        position_id=position.id,
        rule="stop",
    )
    FillIngestService(conn).ingest(
        _snap([_order(sell, "AAA", price="90", order_id="ord-2")]), FILLED
    )
    with pytest.raises(TrdError, match="human sleeve"):
        human.buy("AAA", FILLED + timedelta(minutes=5))


# ------------------------------------------------------------- the scan and exits


def test_the_scan_never_counts_or_buys_a_human_name(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA", "BBB", max_positions=1)
    _bought(engine, human, conn, "AAA")

    result = engine.scan(at=FILLED + timedelta(minutes=5))

    bought = [r.intent.instrument_id for r in result.intents if r.intent.side == Side.BUY]
    aaa = InstrumentRepo(conn).get_by_symbol("AAA")
    assert aaa is not None and aaa.id not in bought
    # The engine's one slot is still free: the human position is not in it.
    assert len(bought) == 1


def test_the_engine_stops_out_a_human_position(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _, position = _bought(engine, human, conn, "AAA", price="100")
    provider.add_symbol("AAA", price=str(position.stop_price - D("1")))

    result = engine.scan(at=FILLED + timedelta(minutes=5))

    sells = [r.intent for r in result.intents if r.intent.side == Side.SELL]
    assert len(sells) == 1 and sells[0].rule == "stop"
    assert sells[0].position_id == position.id


def test_let_run_skips_the_target_but_not_the_stop(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _, position = _bought(engine, human, conn, "AAA", price="100")
    at = FILLED + timedelta(minutes=5)
    provider.add_symbol("AAA", price=str(position.target_price + D("1")))

    assert human.let_run("AAA", at) is True
    assert human.let_run("AAA", at) is False  # already on
    assert [r for r in engine.scan(at=at).intents if r.intent.side == Side.SELL] == []

    # Through the stop it still leaves.
    provider.add_symbol("AAA", price=str(position.stop_price - D("1")))
    sells = [r.intent for r in engine.scan(at=at).intents if r.intent.side == Side.SELL]
    assert [s.rule for s in sells] == ["stop"]


def test_let_run_off_puts_the_target_back(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _, position = _bought(engine, human, conn, "AAA", price="100")
    at = FILLED + timedelta(minutes=5)
    human.let_run("AAA", at)
    assert human.let_run("AAA", at, on=False) is True
    provider.add_symbol("AAA", price=str(position.target_price + D("1")))
    sells = [r.intent for r in engine.scan(at=at).intents if r.intent.side == Side.SELL]
    assert [s.rule for s in sells] == ["target"]


def test_let_run_is_refused_on_an_engine_position(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _filled_position(engine, conn, "AAA")
    with pytest.raises(TrdError, match="engine position"):
        human.let_run("AAA", MONDAY)


# ------------------------------------------------------------ halt and loss stops


def test_an_engine_halt_still_hands_over_human_orders(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA", "BBB")
    engine.halt("testing", MONDAY)
    human.buy("AAA", MONDAY)
    outbox = OutboxService(conn).pending(MONDAY)
    assert outbox.halted and [o.symbol for o in outbox.orders] == ["AAA"]


def test_a_human_halt_withholds_human_buys_never_exits(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA", "BBB")
    _, position = _bought(engine, human, conn, "AAA", price="100")
    human.buy("BBB", FILLED)
    sleeve = engine.humans.get(engine.account().id)
    assert sleeve is not None
    engine.humans.save(sleeve.model_copy(update={"halted": True, "halt_reason": "x"}), FILLED)
    provider.add_symbol("AAA", price=str(position.stop_price - D("1")))
    engine.scan(at=FILLED + timedelta(minutes=1))

    outbox = OutboxService(conn).pending(FILLED + timedelta(minutes=2))

    assert [(o.symbol, o.side) for o in outbox.orders] == [("AAA", Side.SELL)]
    assert [w.order.symbol for w in outbox.withheld] == ["BBB"]


def test_a_human_loss_never_halts_the_engine_and_trips_its_own_stop(engine, human, conn, provider):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA"])  # BBB is outside the engine's universe
    human.set(MONDAY, slots=2, capital=HUMAN_CAPITAL, max_loss=D("100"))
    _bought(engine, human, conn, "AAA", price="100")
    # Marked well below the fill: far past the human loss stop.
    provider.add_symbol("AAA", price="80")

    engine.scan(at=FILLED + timedelta(minutes=1))
    assert not engine.guards.guard(engine.config()).halted  # type: ignore[union-attr]

    with pytest.raises(TrdError, match="human loss stop"):
        human.buy("BBB", FILLED + timedelta(minutes=2))
    sleeve = engine.humans.get(engine.account().id)
    assert sleeve is not None and sleeve.halted
    human.resume(FILLED)
    sleeve = engine.humans.get(engine.account().id)
    assert sleeve is not None and not sleeve.halted


# ------------------------------------------------------------------ scorecards


def test_human_trades_are_left_out_of_the_strategy_scorecard(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _, position = _bought(engine, human, conn, "AAA", price="100")
    closed = position.model_copy(
        update={"status": "closed", "exit_price": D("110"), "exit_rule": "target"}
    )
    assert strategy_stats([closed], {HUMAN_STRATEGY: 1}) == []


# ----------------------------------------------------------------------- backup


def test_backup_round_trips_the_sleeve_and_let_run(tmp_path: Path, engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _bought(engine, human, conn, "AAA")
    human.let_run("AAA", FILLED)
    human.set(MONDAY, bridge=D("250"))

    data = export_data(conn)
    assert data["version"] == 13
    fresh = connect(tmp_path / "fresh.duckdb")
    restore_data(fresh, data)

    restored = EngineService(fresh, provider)
    sleeve = restored.humans.get(restored.account().id)
    assert sleeve is not None
    assert (sleeve.slots, sleeve.capital, sleeve.bridge) == (2, HUMAN_CAPITAL, D("250"))
    assert len(restored.let_run.ids()) == 1
    # And the cash split replays the same from the restored intents and txns.
    a = engine.human_cash(FILLED.date())
    b = restored.human_cash(FILLED.date())
    assert a is not None and b is not None and a.settled == b.settled
    assert TransactionRepo(fresh).list_chronological(restored.account().id)


# -------------------------------------------------------------------------- cli


def test_cli_human_set_and_show(cli_env: FakeProvider):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="205")
    created = runner.invoke(
        app,
        [
            "engine",
            "init",
            "--live",
            "--account",
            "brokerage",
            "--capital",
            "5000",
            "--symbols",
            "AAA",
        ],
    )
    assert created.exit_code == 0, created.output

    result = runner.invoke(
        app, ["engine", "human", "set", "--slots", "2", "--capital", "40", "--json"]
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["sleeve"]["slots"] == 2 and doc["capital_total"] == "5040.000000"

    shown = runner.invoke(app, ["engine", "human"])
    assert shown.exit_code == 0, shown.output
    assert "Human slots" in shown.output and "per slot" in shown.output

    bad = runner.invoke(app, ["engine", "human", "run", "AAA", "--json"])
    assert bad.exit_code == 1 and "No open human position" in bad.output


# ------------------------------------------------------------- review follow-ups


def _runner_filter() -> str:
    """The halted-outbox filter exactly as deploy/live-runner.sh runs it."""
    script = (Path(__file__).parent.parent / "deploy" / "live-runner.sh").read_text()
    (line,) = [ln for ln in script.splitlines() if ln.startswith("py 'import json,sys; d=json")]
    return line.split("'")[1]


def test_the_runner_places_human_orders_while_the_engine_is_halted(tmp_path: Path):
    """The outbox hands over human orders while halted; the runner's own second
    filter must not drop them — a human stop-out left unplaced runs until 16:00."""
    import subprocess
    import sys

    outbox = {
        "halted": True,
        "orders": [
            {"symbol": "AAA", "side": "sell", "rule": "manual", "strategy": "momentum"},
            {"symbol": "BBB", "side": "sell", "rule": "stop", "strategy": "human"},
            {"symbol": "CCC", "side": "buy", "rule": None, "strategy": "human"},
            {"symbol": "DDD", "side": "sell", "rule": "stop", "strategy": "momentum"},
        ],
    }
    src, dst = tmp_path / "outbox.json", tmp_path / "specs.json"
    src.write_text(json.dumps(outbox))
    subprocess.run([sys.executable, "-c", _runner_filter(), str(src), str(dst)], check=True)
    assert [s["symbol"] for s in json.loads(dst.read_text())] == ["AAA", "BBB", "CCC"]


def test_compare_cash_reads_both_sleeves(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _bought(engine, human, conn, "AAA", price="100")
    account = engine.account_cash(FILLED.date())
    sleeves = (engine.cash(FILLED.date()), engine.human_cash(FILLED.date()))
    assert account is not None and all(c is not None for c in sleeves)
    assert account.settled == sum((c.settled for c in sleeves if c), D(0))


def test_the_scan_trips_the_human_loss_stop(engine, human, conn, provider):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    human.set(MONDAY, slots=2, capital=HUMAN_CAPITAL, max_loss=D("100"))
    _bought(engine, human, conn, "AAA", price="100")
    provider.add_symbol("AAA", price="80")

    result = engine.scan(at=FILLED + timedelta(minutes=1))

    assert any("human loss stop" in s for s in result.skipped)
    sleeve = engine.humans.get(engine.account().id)
    assert sleeve is not None and sleeve.halted
    guard = engine.guards.guard(engine.config())
    assert guard is not None and not guard.halted


def test_a_hand_set_loss_stop_survives_a_capital_change(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    human.set(MONDAY, max_loss=D("750"))
    human.set(MONDAY, capital=D("12000"))
    sleeve = engine.humans.get(engine.account().id)
    assert sleeve is not None and sleeve.max_loss == D("750")
    # The default follows the capital.
    human.set(MONDAY, max_loss=D("2400"))  # = 20% of 12,000: reads as the default
    human.set(MONDAY, capital=D("15000"))
    sleeve = engine.humans.get(engine.account().id)
    assert sleeve is not None and sleeve.max_loss == D("3000")


def test_human_capital_cannot_drop_below_what_the_sleeve_spent(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _bought(engine, human, conn, "AAA", price="100")
    with pytest.raises(TrdError, match="tied up"):
        human.set(FILLED, capital=D("1000"))


def test_status_counts_engine_slots_only(engine, human, conn, provider):
    _sleeve(engine, human, conn, provider, "AAA")
    _bought(engine, human, conn, "AAA")
    status = engine.status()
    assert status.open_positions == 0 and status.human_positions == 1
    assert status.capacity == status.max_positions
