# Robinhood MCP — read-only

Connects a Claude Code session to Robinhood's agentic MCP so it can *read* the
brokerage: balances, positions, quotes. Every write tool — placing an order,
cancelling one, exercising an option, mutating a watchlist — is denied by
policy, not by good intentions.

**trd's engine does not use this.** The engine calling a broker is a separate,
deliberately harder problem ([SB-515]); nothing in `src/trd` imports or knows
about MCP. This is agent-side only.

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
34 reads allowed, 19 denied. `deny` beats `allow`, so a tool in both stays
blocked. Permission rules match MCP tools as `mcp__<server>` (all tools) or
`mcp__<server>__<tool>` (exactly one) — there is no mid-name wildcard, so each
tool needs its own line. That is the point: a tool Robinhood adds next month
matches neither list, so it is neither silently allowed nor silently denied — it
surfaces as an unlisted tool that requires an explicit decision.

The 19 denied are the 17 that mutate broker state — `place_equity_order`,
`place_option_order`, `cancel_equity_order`, `cancel_option_order`,
`exercise_option`, `cancel_option_exercise`, the six watchlist mutations and the
four scan mutations — plus `review_equity_order` and `review_option_order`.

The two `review_*` tools do not place anything; they price an order and return
pre-trade alerts. They are denied anyway. trd decides what to trade from its own
data, so the order path has no read that trd needs, and leaving the
order-shaped surface reachable invites a session to drift toward it one harmless
call at a time.

### Re-enumerating the tool list

The list above is a snapshot. To re-derive it — from the server, not from
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

Don't, for now. The ticket that built this gate ([SB-500]) explicitly excludes
order placement even behind a flag. When that changes, the opt-in is an explicit
per-tool `allow` entry plus removing that tool from `deny` — one tool at a time,
in the **committed** `.claude/settings.json`, reviewed as a diff. Not
`settings.local.json`: a lifted ban is exactly the change that must be visible
in version control on every machine, not just the one that made it.

## Reconciliation

The payoff. Read the broker in a session, write a snapshot, then diff it against
what trd believes it holds:

```bash
trd engine reconcile ~/.trd/broker-2026-08-03.json --account rh-agent
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
  "account": "rh-agent",
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

[SB-500]: https://linear.app/silverbeer/issue/SB-500
[SB-515]: https://linear.app/silverbeer/issue/SB-515
