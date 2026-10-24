"""The live runner's transcript reader (deploy/rh_stream.py).

It is the only thing between a model's broker calls and trd's books, so what is
worth pinning is what it refuses: an order call no spec asked for, a review with
checks on it, a login that lapsed, a snapshot from the wrong account. Shapes are
the broker's own, as measured 2026-10-22 on the agentic account.
"""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "rh_stream", Path(__file__).parent.parent / "deploy" / "rh_stream.py"
)
assert SPEC and SPEC.loader
rh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rh)

ACCT = "111111111"
P = "mcp__robinhood-trading__"


def order_spec(intent_id: int = 7, **overrides) -> dict:
    return {
        "intent_id": intent_id,
        "ref_id": f"ref-{intent_id}",
        "symbol": "NVDA",
        "side": "buy",
        "type": "market",
        "quantity": "0.104712",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_price": "191.00",
        **overrides,
    }


def write_stream(
    tmp_path: Path, calls: list[tuple[str, dict, object, bool]], status="connected"
) -> str:
    """A stream-json transcript: init, then each (tool, input, result, is_error)."""
    lines: list[dict[str, Any]] = [
        {
            "type": "system",
            "subtype": "init",
            "mcp_servers": [{"name": "robinhood-trading", "status": status}],
        }
    ]
    for i, (name, args, result, is_error) in enumerate(calls):
        tid = f"toolu_{i}"
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]
                },
            }
        )
        text = result if isinstance(result, str) else json.dumps(result)
        lines.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": text,
                            "is_error": is_error,
                        }
                    ]
                },
            }
        )
    lines.append({"type": "result", "subtype": "success", "result": "DONE"})
    path = tmp_path / "stream.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return str(path)


SETTING_ON = {"data": {"setting": {"account_number": ACCT, "human_must_approve_trades": True}}}


def review_args(spec: dict) -> dict:
    return rh.args_for(spec, ACCT, "review")


def place_args(spec: dict) -> dict:
    return rh.args_for(spec, ACCT, "place")


# ------------------------------------------------------------------ probe


def test_probe_ok_off_and_signed_out(tmp_path: Path) -> None:
    ok = write_stream(
        tmp_path, [(P + "get_trade_approval_setting", {"account_number": ACCT}, SETTING_ON, False)]
    )
    assert rh.probe(rh.load(ok))["status"] == "OK"

    off = {"data": {"setting": {"human_must_approve_trades": False}}}
    path = write_stream(tmp_path, [(P + "get_trade_approval_setting", {}, off, False)])
    assert rh.probe(rh.load(path))["kind"] == "approvals_off"

    signed_out = write_stream(tmp_path, [], status="needs-auth")
    result = rh.probe(rh.load(signed_out))
    assert (result["status"], result["kind"]) == ("FAIL", "auth")
    assert "needs-auth" in result["detail"]

    lapsed = write_stream(
        tmp_path, [(P + "get_trade_approval_setting", {}, "HTTP 401 Unauthorized", True)]
    )
    assert rh.probe(rh.load(lapsed))["kind"] == "auth"


# ------------------------------------------------------------------ review


def test_review_passes_clean_specs_and_blocks_the_rest(tmp_path: Path) -> None:
    clean, checked, unreviewed = (
        order_spec(1),
        order_spec(2, symbol="AMD"),
        order_spec(3, symbol="MU"),
    )
    path = write_stream(
        tmp_path,
        [
            (P + "get_trade_approval_setting", {"account_number": ACCT}, SETTING_ON, False),
            (P + "review_equity_order", review_args(clean), {"data": {"order_checks": []}}, False),
            (
                P + "review_equity_order",
                review_args(checked),
                {"data": {"order_checks": [{"type": "insufficient_buying_power"}]}},
                False,
            ),
        ],
    )
    result = rh.review(rh.load(path), [clean, checked, unreviewed], ACCT)
    assert result["approvals_on"] is True
    assert result["clean"] == [1]
    why = {b["intent_id"]: b["why"] for b in result["blocked"]}
    assert "insufficient_buying_power" in why[2]
    assert why[3] == "not reviewed"
    assert result["unexpected"] == []


def test_a_review_of_a_different_size_is_not_a_review_of_this_order(tmp_path: Path) -> None:
    spec = order_spec(1)
    path = write_stream(
        tmp_path,
        [(P + "review_equity_order", {**review_args(spec), "quantity": "1"}, {"data": {}}, False)],
    )
    assert rh.review(rh.load(path), [spec], ACCT)["clean"] == []


def test_a_place_during_review_is_unexpected(tmp_path: Path) -> None:
    spec = order_spec(1)
    path = write_stream(tmp_path, [(P + "place_equity_order", place_args(spec), {}, False)])
    assert rh.review(rh.load(path), [spec], ACCT)["unexpected"]


# ------------------------------------------------------------------ place


def test_an_approval_becomes_an_approval_ack(tmp_path: Path) -> None:
    spec = order_spec(7)
    approval = {"data": {"approval": {"approval_id": "appr-1", "state": "APPROVAL_STATE_PENDING"}}}
    path = write_stream(tmp_path, [(P + "place_equity_order", place_args(spec), approval, False)])
    result = rh.placed(rh.load(path), [spec], ACCT)
    assert result["acks"] == [
        {
            "intent_id": 7,
            "symbol": "NVDA",
            "side": "buy",
            "quantity": "0.104712",
            "kind": "approval",
            "value": "appr-1",
        }
    ]
    assert result["missing"] == [] and result["unexpected"] == []


def test_an_order_id_and_an_error_are_acked_as_such(tmp_path: Path) -> None:
    filled, failed = order_spec(1), order_spec(2, symbol="AMD")
    order = {"data": {"order": {"id": "ord-1", "state": "queued", "symbol": "NVDA"}}}
    path = write_stream(
        tmp_path,
        [
            (P + "place_equity_order", place_args(filled), order, False),
            (P + "place_equity_order", place_args(failed), "market closed", True),
        ],
    )
    acks = {a["intent_id"]: a for a in rh.placed(rh.load(path), [filled, failed], ACCT)["acks"]}
    assert (acks[1]["kind"], acks[1]["value"]) == ("order", "ord-1")
    assert (acks[2]["kind"], acks[2]["value"]) == ("error", "market closed")


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": "1.0"},  # a different size
        {"symbol": "TSLA"},  # a different name under our ref_id
        {"ref_id": "invented"},  # a ref_id nobody minted
        {"account_number": "222222222"},  # the margin account
        {"dollar_amount": "100.00"},
    ],
)
def test_an_order_call_no_spec_explains_is_unexpected(tmp_path: Path, change: dict) -> None:
    spec = order_spec(7)
    path = write_stream(
        tmp_path, [(P + "place_equity_order", {**place_args(spec), **change}, {"data": {}}, False)]
    )
    result = rh.placed(rh.load(path), [spec], ACCT)
    assert result["unexpected"]
    assert result["acks"] == []
    assert result["missing"] == [7]


# ------------------------------------------------------------------ snapshot


def test_the_snapshot_is_the_brokers_rows_as_returned(tmp_path: Path) -> None:
    row = {
        "id": "6ad8b159",
        "ref_id": "01a15e73",
        "symbol": "ABBV",
        "side": "sell",
        "state": "filled",
        "cumulative_quantity": "0.051266",
        "average_price": "287.630000",
        "executions": [
            {"price": "287.63", "quantity": "0.051266", "timestamp": "2026-10-21T13:30:01.493Z"}
        ],
    }
    approval = {"approval_id": "appr-1", "state": "APPROVAL_STATE_APPROVED"}
    path = write_stream(
        tmp_path,
        [
            (
                P + "get_equity_orders",
                {"account_number": ACCT},
                {"data": {"orders": [row]}, "guide": "..."},
                False,
            ),
            (
                P + "get_trade_approvals",
                {"account_number": ACCT},
                {"data": {"approvals": [approval]}},
                False,
            ),
        ],
    )
    snap = rh.snapshot(rh.load(path), ACCT)
    assert snap is not None
    assert snap["orders"] == [row] and snap["approvals"] == [approval]

    from trd.models.fills import FillSnapshot

    parsed = FillSnapshot.model_validate(snap)
    assert parsed.orders[0].ref_id == "01a15e73"
    assert parsed.approvals[0].state == "APPROVED"


def test_no_snapshot_from_a_failed_read_or_another_account(tmp_path: Path) -> None:
    failed = write_stream(
        tmp_path, [(P + "get_equity_orders", {"account_number": ACCT}, "boom", True)]
    )
    assert rh.snapshot(rh.load(failed), ACCT) is None
    other = write_stream(
        tmp_path,
        [
            (
                P + "get_equity_orders",
                {"account_number": "222222222"},
                {"data": {"orders": []}},
                False,
            )
        ],
    )
    assert rh.snapshot(rh.load(other), ACCT) is None
