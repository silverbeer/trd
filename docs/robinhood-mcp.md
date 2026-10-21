# Robinhood MCP

Connects a Claude Code session to Robinhood's agentic MCP so it can read the
brokerage — balances, positions, quotes, orders — and, for the live engine
only, place and cancel **equity** orders. Every other write tool — options,
advanced and crypto orders, option exercise, watchlist, scan and alert
mutations — is denied by policy, not by good intentions. The three equity
order tools are `ask`: every call prompts in Claude Code, and with trade
approvals on the order also waits for the customer in the Robinhood app.

**trd itself never calls the broker.** Nothing in `src/trd` imports or knows
about MCP. A live engine writes order intents; an agent session drains them
through the MCP and writes the broker's answers back as files trd reads
(see "Draining the outbox" and "Ingesting fills" below). This is agent-side
only.

## Why the split exists

`POST https://agent.robinhood.com/mcp/trading` answers `401` with an OAuth
resource-metadata pointer. The authorization server supports
`authorization_code` and `refresh_token` with PKCE, `token_endpoint_auth: none`,
and dynamic client registration — but **no `client_credentials`**. The first
token always requires a human at a browser.

That is the whole reason the agent-side path works and the unattended engine
path does not: a k3s CronJob has nobody to click "Sign in".

## Setup

### 1. Deny the writes first

`.claude/settings.json` — the **committed** project file, not
`settings.local.json` — carries the gate. It is in place before the server is
added, so no write tool is ever callable, not even by accident during setup.

It is committed on purpose. `settings.local.json` is globally gitignored, so a
gate that lived there would exist on exactly one machine and silently not exist
on any fresh clone — the failure mode being "writes are allowed and nobody
said so". Denying a server that a given checkout has not configured costs
nothing.

Bootstrapping is deliberately two-stage, because the exact tool names are not
knowable until the server answers. **Stage one**, before the server is added,
denies the whole server:

```json
{ "permissions": { "deny": ["mcp__robinhood-trading"] } }
```

That blocks the reads too, which is correct for a checkout that has not been
through stage two — nothing is trusted before it has been named.

**Stage two**, once authenticated, replaces it with every tool listed by name:
53 reads allowed, 3 asked, 27 denied. `deny` beats `allow`, so a tool in both stays
blocked. Permission rules match MCP tools as `mcp__<server>` (all tools) or
`mcp__<server>__<tool>` (exactly one) — there is no mid-name wildcard, so each
tool needs its own line. That is the point: a tool Robinhood adds next month
matches neither list, so it is neither silently allowed nor silently denied — it
surfaces as an unlisted tool that requires an explicit decision.

The 3 asked are the equity order path the live swing engine needs (SB-1373):
`review_equity_order`, `place_equity_order` and `cancel_equity_order`. `ask`
prompts on every call, so each order is approved twice — once in Claude Code,
once in the Robinhood app, where trade approvals are on for the agentic account
(with approvals on, the agent proposes and the customer places). An `allow`
here would leave the app as the only gate.

The 27 denied are the 24 other writes — `place_option_order`,
`place_advanced_order`, `place_crypto_order`, `cancel_option_order`,
`cancel_advanced_order`, `cancel_crypto_order`, `exercise_option`,
`cancel_option_exercise`, `decline_trade_approval`, the six watchlist
mutations, the four scan mutations and the four alert mutations
(`create_alert`, `update_alert`, `delete_alert`, `mark_alerts_read`) — plus the
three that price a non-equity order without placing it: `review_option_order`,
`review_advanced_order` and `preview_crypto_order`.

The order previews do not place anything; they price an order and return
pre-trade alerts. The equity one is asked because the place workflow runs
review first; the other three are denied anyway. trd decides what to trade from its own
data, so the order path has no read that trd needs, and leaving the
order-shaped surface reachable invites a session to drift toward it one harmless
call at a time.

### Re-enumerating the tool list

The list above is a snapshot, and it drifts. Between SB-500 and 2026-10-20
Robinhood added 30 tools — advanced (OCO) orders, crypto orders, trade
approvals, alerts, SEC filings — 11 of them writes, and for that whole time
they sat on neither list (SB-1364). An unlisted tool is not a denied one: in
auto mode the classifier decides, not this file. Re-enumerate whenever
`/mcp` shows tools you do not recognise, and before any change to the order
path.

Claude Code hides a denied tool from the session entirely, so a session's own
tool list cannot tell you whether a denied tool still exists on the server.
Only `tools/list` can. To re-derive it — from the server, not from
documentation, and without calling a single tool — issue a JSON-RPC
`initialize` followed by `tools/list` against the endpoint with the stored OAuth
token. Never read that token by a means that prints it to a terminal or a
transcript; pipe it. `security find-generic-password -s "Claude Code-credentials" -g`
prints the secret and will leak both the Robinhood tokens and the Claude Code
session token — use `-w` and pipe it into the request.

### 2. Add the server, at **local** scope

```bash
claude mcp add --transport http robinhood-trading https://agent.robinhood.com/mcp/trading
```

Local is the default and the right one. `user` scope would put brokerage tools
in every unrelated session on the machine; `project` scope writes `.mcp.json`
into the repo, which puts a broker endpoint in version control. Local keeps it
to this checkout and out of git.

### 3. Sign in

Run `/mcp` in Claude Code, pick `robinhood-trading`, authenticate in the
browser. Tokens are stored by Claude Code, not by trd, and never touch this
repo.

### 4. Confirm which mode you are in

```bash
claude mcp list
```

- `✔ Connected` — authenticated; read tools work, write tools are refused by policy
- `! Needs authentication` — no token; nothing works
- `✘ Failed to connect` — endpoint or network problem

## Lifting the write ban

Done once, for the equity order path only (SB-1373, PR #143):
`review_equity_order`, `place_equity_order` and `cancel_equity_order` moved
from `deny` to `ask` — not `allow`, so the Robinhood app's trade approval is
never the only gate. Any further write follows the same rule: one tool at a
time, out of `deny`, in the **committed** `.claude/settings.json`, reviewed as
a diff. Not
`settings.local.json`: a lifted ban is exactly the change that must be visible
in version control on every machine, not just the one that made it.

## Reconciliation

The payoff. Read the broker in a session, write a snapshot, then diff it against
what trd believes it holds:

```bash
trd engine reconcile ~/.trd/broker-2026-08-03.json --account ***REMOVED***
```

The read and the diff are deliberately separate. Reading a brokerage is
authenticated, interactive and agent-side; the diff is arithmetic. Splitting
them means the comparison is reproducible, testable without a broker account,
and cannot smuggle a network call into a service that must never make one — the
same reason all market data goes through `MarketDataProvider`.

### Snapshot format

```json
{
  "as_of": "2026-08-03T14:22:00-04:00",
  "source": "robinhood",
  "account": "***REMOVED***",
  "cash": "1204.11",
  "positions": [
    {"symbol": "AMZN", "quantity": "3.759964", "price": "284.56"},
    {"symbol": "MU",   "quantity": "1.263104", "price": "796.21"}
  ]
}
```

`as_of` is load-bearing: a snapshot taken before the last engine fill shows a
gap that is timing, not error. `price` is optional — without it the price
columns read `—` and only share counts are compared.

### Reading it

| Status | Meaning |
|---|---|
| `ok` | Both sides agree on the share count |
| `QUANTITY` | Both hold it, different sizes |
| `MISSING AT BROKER` | trd believes it holds this; the broker does not |
| `UNTRACKED` | The broker holds it; trd has never heard of it |

Problems sort to the top. The command exits **non-zero** when anything
disagrees, so a scheduled check can act on it.

`trd px` carries the date of the stored close it came from. A wide price gap
next to matching share counts is a stale-bar problem (`trd sync`), not a
bookkeeping one — which is why the date is printed next to the number.

Broker cash is reported but never diffed: trd tracks positions, not cash, so a
"delta" there would be invented.

## Ingesting fills

A live engine (`trd engine init --live`) writes order intents and fills nothing
itself. What the broker did with them comes back the same way reconciliation
does: read in a session, written to a file, applied by trd with no network.

Two **read** tools produce the snapshot, both already on the allow list:

- `get_equity_orders` → `"orders"`
- `get_trade_approvals` → `"approvals"`

Write their results into one JSON file **as-is** — no reshaping, no renaming,
unknown fields are ignored — plus an optional `taken_at`:

```json
{
  "taken_at": "2026-10-21T14:05:00Z",
  "orders": [ ...get_equity_orders rows... ],
  "approvals": [ ...get_trade_approvals rows... ]
}
```

```bash
trd engine fills ~/.trd-live/fills-2026-10-21.json
```

An order belongs to an intent when its `ref_id` is the intent's `ref_id` **or
its `approval_id`**: with trade approvals on, the broker stamps the eventual
order with the approval's id, not the one trd sent (observed 2026-10-21). Orders
and approvals that match no intent — manual trades, history — are listed and
left alone. Re-running on the same file changes nothing; anything contradictory
(symbol or side disagree, a sell for a position already closed) is shown, never
applied, and the command exits non-zero.

`--notify` posts each booked fill to the channel (`TELEGRAM_CHAT_ID`) — side,
size, fill price, what the scan saw and the slippage between them, plus realized
P&L and R on a sell — and anything rejected, expired or conflicting to the live
alert chat (below).

## Draining the outbox (live engine)

A live scan writes order intents and places nothing. An agent session places
them, through the three equity order tools that are `ask` (never `allow`) in
`.claude/settings.json`, and tells trd what came back. trd never calls the broker:
it emits a spec and records an id.

Trade only the **agentic account** — the one `get_accounts` reports as the account
the agent may trade. Its account number is the one every order call below takes.

Follow these steps exactly, in order.

1. **Read the outbox.**

   ```bash
   TRD_HOME=~/.trd-live trd engine outbox --json
   ```

   `orders` is every PENDING intent still actionable, oldest first. Each carries
   `intent_id`, `ref_id`, `symbol`, `side`, `quantity` (a 6dp string), `type`
   (`market`), `time_in_force` (`gfd`) and `market_hours` (`regular_hours`) — the
   order parameters, unchanged — plus `ref_price`, `notional`, `reason`,
   `created_at` and `expires_at` for the person approving it. An empty `orders`
   means there is nothing to do: stop.

2. **Review each spec.** Call `review_equity_order` with the same parameters. If
   `order_checks` is non-empty, **stop and show the user**. Do not place it, do not
   ack it; it stays pending and lapses on its own at 16:00.

3. **Place it.** Call `place_equity_order` with the same parameters and the
   spec's `ref_id` as the order's `ref_id`. The `ref_id` is the broker's
   idempotency key: sending it twice cannot make two orders.

4. **Record the answer** — exactly one of these per spec, with the `intent_id` (or
   the `ref_id`):

   - An **approval** (trade approvals on, the normal case): the broker holds the
     order until the user approves it in the app.

     ```bash
     TRD_HOME=~/.trd-live trd engine outbox ack <intent_id> --approval-id <approval_id> --notify
     ```

     `--notify` sends the user "approve … in the Robinhood app — expires 16:00 ET"
     in the live alert chat.
   - An **order** (no approval needed): `--broker-order-id <order id>`.
   - An **error** (the place call failed, nothing reached the broker):
     `--error "<the error text>"`.

   The same ack twice is a no-op. An ack against an intent already resolved, or
   with a different approval or order id than the one recorded, is refused and
   exits 1 — show it to the user rather than working around it.

5. **Never re-place with a new `ref_id`.** While an approval is pending the order
   exists; a second place is a second order. A retry after a failed place reuses
   the spec's own `ref_id` — trd never mints another one for the same intent.

6. **Later, bring the outcome back.** Call `get_equity_orders` and
   `get_trade_approvals`, write the results as-is to one file as
   `{"orders": [...], "approvals": [...]}`, then:

   ```bash
   TRD_HOME=~/.trd-live trd engine fills fills.json --notify
   TRD_HOME=~/.trd-live trd engine reconcile positions.json
   ```

   The fill ingest books the fills (an approved order carries the approval's id as
   its `ref_id`, which is how it finds the intent), and reconcile — fed a
   positions snapshot ([Reconciliation](#reconciliation)) — confirms the book and
   the broker agree.

### Timing

- Every intent expires at the **16:00 ET** close of the session it was written
  in, with its trade approval. After that the outbox no longer lists it and the
  next scan or fill ingest marks it expired.
- An intent written at or after **15:45 ET** comes out with `"late": true` and a
  `note` saying how many minutes are left. Place it if you can, but tell the user
  the approval has minutes, not hours.

### The database is single-writer

`outbox` only reads. `outbox ack` and `fills` write, and DuckDB allows one writer:
if a scan holds the lock, they wait briefly and then fail with
`DatabaseBusyError`. That is not a failure of the order — retry after a few
seconds. Never re-place an order because an ack failed; the ack is what to retry.

### Where the messages go

| Moment | Destination |
|---|---|
| a live scan writes an intent | alert chat |
| `outbox ack --approval-id … --notify` | alert chat |
| `fills --notify` books a fill | channel (`TELEGRAM_CHAT_ID`) |
| `fills --notify` finds something rejected, expired or conflicting | alert chat |

The alert chat is `TRD_LIVE_ALERT_CHAT_ID`; unset, the first id in
`TRD_BOT_ALLOWED_USER_IDS` (a private chat with the bot has the user's own id);
unset too, `TELEGRAM_CHAT_ID` with a warning. Same bot token throughout. Live
messages are labelled `💵 live` unless `TRD_ENGINE_LABEL` names the engine.

[SB-500]: https://linear.app/silverbeer/issue/SB-500
[SB-515]: https://linear.app/silverbeer/issue/SB-515
