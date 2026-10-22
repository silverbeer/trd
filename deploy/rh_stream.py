#!/usr/bin/env python3
"""The live runner's reader for a headless Claude session's Robinhood MCP calls.

The runner (deploy/live-runner.sh) has a model make the broker calls, because the
Robinhood MCP's sign-in lives in Claude Code. It never lets the model *report*
what came back. Every session runs with `--output-format stream-json`, and this
reads the transcript: each tool call's exact input, and each result as the broker
returned it. A fill snapshot is the broker's own JSON, never a model's retyping
of it — "written as-is" is the contract `trd engine fills` was built on.

It also checks the model's work. Every order call must match a spec the outbox
emitted (symbol, side, quantity, ref_id, account); anything else is reported as
`unexpected`, and the runner halts the engine on it.

Stdlib only, and not part of `trd`: nothing under src/trd knows about MCP.

    rh_stream.py args     --specs F --account A --tool review|place
    rh_stream.py probe    --stream F
    rh_stream.py review   --stream F --specs F --account A
    rh_stream.py placed   --stream F --specs F --account A
    rh_stream.py snapshot --stream F --account A
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

SERVER = "robinhood-trading"
PREFIX = f"mcp__{SERVER}__"
PLACE = f"{PREFIX}place_equity_order"
REVIEW = f"{PREFIX}review_equity_order"
ORDERS = f"{PREFIX}get_equity_orders"
APPROVALS = f"{PREFIX}get_trade_approvals"
SETTING = f"{PREFIX}get_trade_approval_setting"

# The fields of an outbox spec that are order parameters, passed through as-is.
ORDER_FIELDS = ("symbol", "side", "type", "quantity", "time_in_force", "market_hours")

# What a failed sign-in looks like in a tool error. The init event's server
# status is the reliable signal; this catches a token that lapses mid-session.
AUTH_PATTERN = re.compile(
    r"\b401\b|unauthori[sz]ed|re-?authenticat|token (has )?expired|needs-auth", re.I
)


# ------------------------------------------------------------------ reading


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def load(path: str) -> dict[str, Any]:
    """Every tool call in a stream-json transcript, paired with its result."""
    status: str | None = None
    uses: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    final = ""
    with open(path) as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                for server in event.get("mcp_servers") or []:
                    if server.get("name") == SERVER:
                        status = server.get("status")
            elif kind in ("assistant", "user"):
                content = (event.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get("type") == "tool_use":
                        uses[block["id"]] = {
                            "name": block.get("name", ""),
                            "input": block.get("input") or {},
                            "result": None,
                            "text": "",
                            "is_error": False,
                            "answered": False,
                        }
                        order.append(block["id"])
                    elif block.get("type") == "tool_result":
                        call = uses.get(block.get("tool_use_id", ""))
                        if call is None:
                            continue
                        text = _text(block.get("content"))
                        call.update(
                            text=text,
                            result=_json(text),
                            is_error=bool(block.get("is_error")),
                            answered=True,
                        )
            elif kind == "result":
                final = str(event.get("result") or "")
    return {"mcp_status": status, "calls": [uses[i] for i in order], "final": final}


def rh_calls(stream: dict[str, Any], name: str | None = None) -> list[dict[str, Any]]:
    return [
        c
        for c in stream["calls"]
        if c["name"].startswith(PREFIX) and (name is None or c["name"] == name)
    ]


def auth_problem(stream: dict[str, Any]) -> str | None:
    """Why the session could not use the broker, or None if it could."""
    status = stream["mcp_status"]
    if status is None:
        return "robinhood-trading MCP not loaded in the session"
    if status != "connected":
        return f"robinhood-trading MCP status: {status}"
    for call in rh_calls(stream):
        if call["is_error"] and AUTH_PATTERN.search(call["text"]):
            return f"{call['name'].removeprefix(PREFIX)}: {call['text'][:160]}"
    return None


def _find(value: Any, key: str) -> Any:
    """The first value under `key`, anywhere in a JSON document."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find(child, key)
            if found is not None:
                return found
    return None


def _order_id(value: Any) -> str | None:
    """The id of the first thing in a result that looks like an equity order."""
    if isinstance(value, dict):
        if isinstance(value.get("id"), str) and "state" in value and "symbol" in value:
            return value["id"]
        for child in value.values():
            found = _order_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _order_id(child)
            if found:
                return found
    return None


def approvals_on(stream: dict[str, Any]) -> bool | None:
    """What get_trade_approval_setting said; None when it was not read."""
    for call in rh_calls(stream, SETTING):
        if call["is_error"] or call["result"] is None:
            continue
        value = _find(call["result"], "human_must_approve_trades")
        if isinstance(value, bool):
            return value
    return None


# ------------------------------------------------------------------ matching


def _qty(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return None


def args_for(spec: dict[str, Any], account: str, tool: str) -> dict[str, Any]:
    """The exact arguments the model is told to pass. Review takes no ref_id."""
    args = {"account_number": account, **{k: spec[k] for k in ORDER_FIELDS if k in spec}}
    if tool == "place":
        args["ref_id"] = spec["ref_id"]
    return args


def _same_order(call_input: dict[str, Any], spec: dict[str, Any], account: str) -> bool:
    return (
        str(call_input.get("account_number")) == account
        and str(call_input.get("symbol", "")).upper() == str(spec["symbol"]).upper()
        and str(call_input.get("side", "")).lower() == str(spec["side"]).lower()
        and _qty(call_input.get("quantity")) == _qty(spec["quantity"])
        and str(call_input.get("type", "")).lower() == str(spec.get("type", "")).lower()
        and call_input.get("dollar_amount") in (None, "")
    )


def _wrong_account(stream: dict[str, Any], account: str) -> list[str]:
    return [
        f"{c['name'].removeprefix(PREFIX)} on account {c['input'].get('account_number')!r}"
        for c in rh_calls(stream)
        if "account_number" in c["input"] and str(c["input"]["account_number"]) != account
    ]


def review(stream: dict[str, Any], specs: list[dict[str, Any]], account: str) -> dict[str, Any]:
    """Which specs reviewed clean. A spec with order_checks, an error, or no review
    at all is blocked — the runbook's rule is stop and show the person."""
    calls = rh_calls(stream, REVIEW)
    used: set[int] = set()
    clean: list[int] = []
    blocked: list[dict[str, Any]] = []
    for spec in specs:
        match = next(
            (
                i
                for i, c in enumerate(calls)
                if i not in used and _same_order(c["input"], spec, account)
            ),
            None,
        )
        if match is None:
            blocked.append({**_who(spec), "why": "not reviewed"})
            continue
        used.add(match)
        call = calls[match]
        if call["is_error"] or not call["answered"]:
            blocked.append({**_who(spec), "why": f"review failed: {call['text'][:200]}"})
            continue
        checks = _find(call["result"], "order_checks")
        if checks:
            blocked.append({**_who(spec), "why": f"order checks: {json.dumps(checks)[:300]}"})
            continue
        clean.append(spec["intent_id"])
    return {
        "auth": auth_problem(stream),
        "approvals_on": approvals_on(stream),
        "clean": clean,
        "blocked": blocked,
        "unexpected": _wrong_account(stream, account)
        + [
            f"place_equity_order {c['input'].get('symbol')} during review"
            for c in rh_calls(stream, PLACE)
        ],
    }


def _who(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent_id": spec["intent_id"],
        "symbol": spec["symbol"],
        "side": spec["side"],
        "quantity": spec["quantity"],
    }


def placed(stream: dict[str, Any], specs: list[dict[str, Any]], account: str) -> dict[str, Any]:
    """One ack per spec the model placed, and every order call no spec explains."""
    by_ref = {s["ref_id"]: s for s in specs}
    acks: list[dict[str, Any]] = []
    unexpected = _wrong_account(stream, account)
    seen: set[str] = set()
    for call in rh_calls(stream, PLACE):
        spec = by_ref.get(str(call["input"].get("ref_id")))
        if spec is None or not _same_order(call["input"], spec, account):
            unexpected.append(f"place_equity_order {json.dumps(call['input'])[:300]}")
            continue
        if spec["ref_id"] in seen:
            continue  # a retry under the same ref_id is the same order
        seen.add(spec["ref_id"])
        approval = _find(call["result"], "approval_id") if call["result"] else None
        order_id = _order_id(call["result"]) if call["result"] else None
        if call["is_error"] or not call["answered"]:
            ack = {"kind": "error", "value": (call["text"] or "no result")[:300]}
        elif isinstance(approval, str) and approval:
            ack = {"kind": "approval", "value": approval}
        elif order_id:
            ack = {"kind": "order", "value": order_id}
        else:
            ack = {"kind": "error", "value": f"no approval or order id in: {call['text'][:240]}"}
        acks.append({**_who(spec), **ack})
    missing = [s["intent_id"] for s in specs if s["ref_id"] not in seen]
    return {
        "auth": auth_problem(stream),
        "acks": acks,
        "missing": missing,
        "unexpected": unexpected,
    }


def snapshot(stream: dict[str, Any], account: str) -> dict[str, Any] | None:
    """The fill snapshot, from the last orders and approvals reads, as returned."""
    orders = [c for c in rh_calls(stream, ORDERS) if c["answered"] and not c["is_error"]]
    approvals = [c for c in rh_calls(stream, APPROVALS) if c["answered"] and not c["is_error"]]
    if not orders or _wrong_account(stream, account):
        return None
    order_rows = _find(orders[-1]["result"], "orders")
    approval_rows = _find(approvals[-1]["result"], "approvals") if approvals else []
    if not isinstance(order_rows, list) or not isinstance(approval_rows, list):
        return None
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # noqa: UP017 -- runs on macOS python 3.9
        "orders": order_rows,
        "approvals": approval_rows,
        "more_pages": bool(_find(orders[-1]["result"], "next")),
    }


def probe(stream: dict[str, Any]) -> dict[str, Any]:
    problem = auth_problem(stream)
    if problem:
        return {"status": "FAIL", "kind": "auth", "detail": problem}
    on = approvals_on(stream)
    if on is None:
        return {"status": "FAIL", "kind": "unread", "detail": "approval setting was not read"}
    if not on:
        return {"status": "FAIL", "kind": "approvals_off", "detail": "trade approvals are OFF"}
    return {"status": "OK", "kind": "ok", "detail": "approvals=on"}


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["args", "probe", "review", "placed", "snapshot"])
    parser.add_argument("--stream")
    parser.add_argument("--specs")
    parser.add_argument("--account", default="")
    parser.add_argument("--tool", choices=["review", "place"], default="review")
    opts = parser.parse_args(argv)

    specs: list[dict[str, Any]] = []
    if opts.specs:
        with open(opts.specs) as handle:
            specs = json.load(handle)

    if opts.command == "args":
        out: Any = [args_for(s, opts.account, opts.tool) for s in specs]
    else:
        stream = load(opts.stream)
        if opts.command == "probe":
            out = probe(stream)
        elif opts.command == "review":
            out = review(stream, specs, opts.account)
        elif opts.command == "placed":
            out = placed(stream, specs, opts.account)
        else:
            out = snapshot(stream, opts.account)
            if out is None:
                print(json.dumps({"error": "no usable orders read", "auth": auth_problem(stream)}))
                return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
