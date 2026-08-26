# Driving the engines from Telegram

The engine pushes fills to your chat. The bot makes that chat two-way: you can ask
what the book looks like and change what the engines watch, from a phone, without
opening a terminal.

## Where to type

**The direct chat with `@trd_engine_bot`.** Search the bot's name in Telegram and
open it — you do not create anything, and it is the same chat the trade alerts
arrive in.

Messages in a **group** are ignored, silently and deliberately: a group post
carries no reliable sender to authorize, and the alert channel stays one-way. If
the bot seems dead, this is the first thing to check — you are almost certainly
typing in a group.

Only your numeric Telegram user id can command it. Unknown senders get no reply
at all rather than a refusal: a refusal tells a scanner the bot is live and worth
attacking, while silence is indistinguishable from a dead token.

## Commands

Reads work with or without the slash — `status` and `/status` are the same.
Writes need the slash, because a verb and a symbol are easy to type by accident.

| Command | What you get |
|---|---|
| `status [engine]` | build, capacity, bar depth, P&L, money at risk |
| `book [engine]` | open positions and the scorecard, as last published |
| `positions [engine]` | same as `book` |
| `report [engine]` | per-strategy expectancy over closed trades |
| `engines` | which engines this bot drives |
| `help` | the list |
| `/add SYM [engine ...]` | put a name in the universe |
| `/rm SYM [engine ...]` | take a name out |

### Naming an engine

There are two — **`swing`** (daily bars, holds overnight) and **`day`** (5-minute
bars, flat by the bell). Every command takes an optional engine name:

```
status              both engines
status day          just the day engine
/add PLTR           add to both
/add PLTR swing     add to the swing engine only
```

Defaulting to **both** is the right bias: the universes are meant to agree, and
the common case really is "put this in front of both sets of rules". Naming one
is the exception, so naming is the thing you have to type.

## How fresh the answers are

**Reads answer from the last published snapshot, not from the database.** The bot
never opens DuckDB — it is single-writer, and the bot is resident while a scan is
not, so a connection held here would lock out the trading path and put a chat
feature in the way of a trade. Each scan publishes `status.json` and
`report.json`; the bot reads those. So a read is as fresh as the last scan: up to
five minutes old during the session, and from the 16:00 pass overnight.

**An add is checked before it is queued.** A ticker the provider does not know is
refused in the reply, and nothing is written:

```
/add FISERV
FISERV — no such symbol. Nothing queued; check the ticker and try again.
```

A known one is named back, so a typo that happens to be a real ticker is visible
too: `queued: add INTC (Intel Corporation) → swing, day`. If the provider cannot
be reached the add is queued anyway and the reply says it could not be verified —
a network blip must not block a legitimate add. `/rm` is never checked: dropping a
delisted name is exactly when the provider will not resolve it.

**Writes are queued, not applied.** `/add` and `/rm` drop a file in the engine's
`commands/` directory, and something drains it: the scan during the session
(within five minutes), and a dedicated job every ten minutes outside it. The reply
says which of those applies rather than guessing.

The split exists because DuckDB has one writer and the scan is the process that
safely holds it — so a chat command never opens the database itself, it leaves an
intent for something that already has the lock. A name added from chat is in the
universe for the very next scan, because the queue is drained *before* the scan
rather than after.

`/add` also pulls two years of history for that symbol alone and tells you whether
it clears the engine's warmup — a name in the universe with no bars is skipped
every pass and reads as a broken engine rather than one warming up.

## What it deliberately will not do

**There is no order placement.** The engines trade simulation accounts, and
entries and exits stay with the rules. The bot changes what is *watched*; it does
not decide what is *traded*.

## When it does not answer

| Symptom | Cause |
|---|---|
| No reply at all | Typing in a group, not the direct chat |
| No reply at all | Message came from a user id not on the allowlist |
| `Unknown engine 'x'` | Engine names are `swing` and `day` |
| Numbers look stale | Reads are as fresh as the last scan — check `status` for its timestamp |
| Nothing works, pod restarting | Two pollers. Telegram permits one `getUpdates` per token and 409s the rest — see the k3s README |

Operator-side checks:

```bash
kubectl logs -n trd -l component=bot -f
kubectl exec -n trd deploy/trd-engine-bot -- trd bot check

# and, without needing the cluster at all:
./scripts/telegram.sh check      # is the token live, and whose is it
./scripts/telegram.sh send "hi"  # prove the outbound path
```
