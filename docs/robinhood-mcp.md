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
`settings.local.json` — carries the deny rule. It is in place before the server
is added, so no write tool is ever callable, not even by accident during setup:

```json
{
  "permissions": {
    "deny": ["mcp__robinhood-trading"]
  }
}
```

It is committed on purpose. `settings.local.json` is globally gitignored, so a
gate that lived there would exist on exactly one machine and silently not exist
on any fresh clone — the failure mode being "writes are allowed and nobody
said so". Denying a server that a given checkout has not configured costs
nothing.

The rule denies the **whole server** while the exact tool names are unknown.
Once the server is authenticated and its tool list is visible, narrow it in
`.claude/settings.json` to the write tools by name and allow the reads
explicitly:

```json
{
  "permissions": {
    "allow": ["mcp__robinhood-trading__<read tool>", "..."],
    "deny":  ["mcp__robinhood-trading__<write tool>", "..."]
  }
}
```

`deny` beats `allow`, so a write tool listed in both stays blocked. Permission
rules match MCP tools as `mcp__<server>` (all tools on that server) or
`mcp__<server>__<tool>` (exactly one) — there is no mid-name wildcard, so each
write tool needs its own line. That is deliberate: a new write tool added by
Robinhood next month is *not* silently covered by a pattern, and shows up as an
unlisted tool rather than an allowed one.

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
in `settings.local.json`, reviewed as a diff.

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
