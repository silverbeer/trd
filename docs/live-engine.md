# The live engine — real money at the broker

The swing engine's rules, trading **$100 of real money** in the broker's agentic
account (a cash account): 5 slots of $20, T+1 settled cash, every order
approved by you on your phone.

This is the operator's guide: what it is, how an order travels, how to turn it
on, how to turn it off, and what to do when something goes wrong. The design
behind it is in [broker-mcp.md](broker-mcp.md) ("Draining the outbox", "The
live runner") and the tickets SB-1325 (go/no-go) and SB-1373–1382.

> Facts measured on this account, not recalled: with trade approvals on, the
> order's `ref_id` is the approval's id (2026-10-21); approvals expire at 16:00
> ET; fractional orders are market + regular hours only; sale proceeds settle
> T+1. Recheck anything here against the broker before relying on it for a new
> decision.

---

## What it is

| | |
|---|---|
| Rules | swing's: `breakout`, `macd_cross`, `momentum`, `pullback` on daily bars, same 137 names |
| Size | `--capital 100 --size 20 --max 5` — five $20 positions, fractional shares |
| Account | `live-cash` in `~/.trd-live` = the broker's agentic account. Anything else in that account is idle and **not** the engine's |
| Bridge | optional, `trd engine cash --bridge N` (SB-1445): spare settled cash you keep in the account beyond the capital, so a refill after an exit need not wait out T+1. An entry may spend settled + min(N, proceeds still settling) — it fronts money already on its way, never more, and is never capital (worth and return ignore it). trd cannot see the broker's cash: keep at least N spare there, or the broker rejects the order |
| Paper twin | `swing20` (`~/.trd-swing20`): identical config on paper. The daily report compares them — this is how live fills are judged |
| Human gate | Broker trade approvals **ON**. Nothing executes until you tap Approve |

**A live engine decides; it never trades.** Where the paper engines write a
trade, the live scan writes an **order intent** — side, quantity, the price it
saw, why. Something outside trd has to place it, and trd only learns about the
fill when the broker's records are read back.

## How an order travels

```
k3s  trd-live-scan (every 5 min, 09:30–16:00)
       └─ writes an order intent ──────────────► trd engine outbox
mini launchd  live-runner (every 10 min, 09:30–16:15)
       ├─ review_equity_order        (The broker's pre-trade check)
       ├─ place_equity_order         → held as a trade approval
       ├─ outbox ack --notify        → 📱 Telegram: "approve BUY X … in your broker app"
you    └─ tap Approve in the broker's app  (or Decline, or ignore → expires 16:00)
mini  live-runner, next pass
       ├─ get_equity_orders / get_trade_approvals
       └─ trd engine fills --notify  → 📢 Telegram: "filled BUY X … @ p (scan saw r, ±s%)"
```

The runner is a headless `claude -p` on the mini, because the broker's MCP's
sign-in lives in Claude Code. **The model makes the calls and never reports
them**: `deploy/broker_stream.py` reads what the broker actually returned from the
session transcript, and checks every order call against the intent that asked
for it. An order call nothing asked for halts the engine.

### What you receive

| When | Where | Message |
|---|---|---|
| an order is waiting for you | private chat | `💵 LIVE: approve BUY NVDA 0.104712 sh (~$20.00 @ 191.00) in your broker app — expires 16:00 ET` + the reason |
| it filled | channel | `[💵 live] filled BUY NVDA 0.104712 @ 190.88 (scan saw 191.00, -0.06%)`; a sell adds realized $ and R |
| rejected, expired, or the broker's review flagged it | private chat | what happened; nothing for you to do |
| an **exit** still waiting 15 min (SB-1735) | private chat | `🚨 💵 LIVE: STOP LOSS EXIT waiting 25 min — XYZ …` — waiting on your approval, or not placed. Repeats every 30 min until answered. **No request in the broker app? Sell by hand, then `trd engine fills`** |
| an **exit** expired unfilled at 16:00 (SB-1735) | private chat | `🚨 💵 LIVE: STOP LOSS EXIT EXPIRED unfilled — XYZ is still held`; the next scan in session re-issues it |
| an approval request could not be sent | private chat (runner) | `⚠ trd live runner: intent N is waiting on a broker approval, and the Telegram request did not go out` — check the broker app |
| The broker login lapsed, or approvals switched off | private chat | how to fix it; reminders ≤ every 3h in the session; an all-clear |
| the engine halted itself (loss stop) or the runner halted it | private chat | why, and what to check |
| runner stopped / orders stuck | private chat (watchdog) | `🟠 trd live engine needs a person` |
| every weekday 16:16 | channel | daily report: **💵 LIVE (real money)** block first, then paper; live vs swing20 |
| every weekday 16:20 | private chat | `💵 trd live — <date>`: the account (capital, worth, return, today's move, cash, orders, halt), then every open position — P&L, R, today's move, stop in force (↑ = trailing) and how far below, target, at risk — then the day's buys, exits and orders that never filled. `trd engine live-report`; CronJob `trd-live-report`, applied by `--live`. Nothing is sent on a market holiday |
| every weekday 16:45 | private chat | `🧭 Human-slot brief`: six bounce signals on/off with their numbers, the universe names 10%+ below their 200-session average that have turned (with the stop and target a human buy would get), what is stretched and still falling, and room in the human sleeve. A screen, never an order — measured, it has no edge over a random name. `trd engine human brief`; CronJob `trd-human-brief`, applied by `--live`. Nothing is sent on a market holiday |

`/status live` in the bot shows a halt first, then open intents, awaiting
approval, settled cash and stuck intents.

### The safety rails

| Rail | What it does |
|---|---|
| Trade approvals (The broker) | every order waits for your tap. The runner **refuses to place** if approvals are off |
| Hard caps (`trd engine guard`) | max order $25, max $100 of buys per day, 5 positions, loss stop $20 — independent of the strategy config, checked at the scan and again before placing. Buys only |
| Loss stop | realized + unrealized ≤ −$20 → the engine halts itself and tells you |
| Stale buys | a buy sized off a quote older than 30 min is withheld and re-planned |
| Kill switch | `trd engine halt` — see [Turn it off](#turn-it-off) |
| Runner checks | an order call that doesn't match its intent halts the engine |
| Watchdog | alarms if scans stop, the runner stops (25 min), intents get stuck, or an exit waits 30+ min (SB-1735) |
| Exit reminders | `trd engine nudge`, every runner pass: an exit waiting 15 min is asked about, then every 30. On 2026-11-17 a stop-out's approval push never arrived and it lapsed after six silent hours |
| Login check | hourly; Telegram when the broker's sign-in lapses |

Stops are **not** resting orders at the broker — the broker will not rest a stop
on fractional shares. An exit happens when a scan sees the stop crossed, writes a
sell intent, and you approve it. A gap overnight is taken at the open, as on paper.

---

## Turn it on

Do steps 1–5 with the market closed (after 16:45, or before 09:00). Step 3 pulls
two years of bars for 137 names and takes several minutes. Nothing spends money
until step 6.

**Already in place** (2026-10-22): trade approvals ON, account funded, launchd jobs
`io.silverbeer.trd.live-runner` and `io.silverbeer.trd.broker-auth-probe` loaded,
`~/.config/trd/live.env` written. Check with:

```bash
launchctl list | grep -E 'trd\.(live-runner|broker-auth-probe)'   # both listed, status 0
tail -3 ~/Library/Logs/trd-live-runner.log                    # recent "[probe] OK ok approvals=on"
```

### 1. Deploy current main to k3s

```bash
cd ~/gitrepos/trd && git checkout main && git pull
echo y | ./scripts/deploy-k3s.sh --swing20          # builds the image
echo y | ./scripts/deploy-k3s.sh --skip-build
echo y | ./scripts/deploy-k3s.sh --skip-build --day
kubectl rollout restart deployment/trd-engine-bot -n trd
```

✅ each prints `Image version: … → 0.1.0+<sha of main>`.

### 2. Pin the runner to the same code

```bash
deploy/live-runner.sh install
```

✅ `installed <sha>` — the same sha as step 1. **Re-run this after every k3s
deploy**; the runner never updates itself.

### 3. Create the live home — a copy of swing20

```bash
TWIN=$(TRD_HOME=~/.trd-swing20 trd engine status --json)
SYMS=$(jq -r '.universe | join(",")' <<<"$TWIN")
STRATS=$(jq -r '.strategies | join(",")' <<<"$TWIN")

TRD_HOME=~/.trd-live trd init
TRD_HOME=~/.trd-live trd engine init --live --account live-cash \
    --capital 100 --size 20 --max 5 --symbols "$SYMS" --strategies "$STRATS"
TRD_HOME=~/.trd-live trd sync --full
```

`engine init --live` creates the `live-cash` real account. If swing20 runs with a
non-default earnings blackout or regime gate (`trd engine status` shows them),
pass the same values — a twin that differs in a gate is not a twin.

### 4. Check before deploying

```bash
TRD_HOME=~/.trd-live trd engine status    # mode live · 5 × $20 · 137 names · no warm-up warnings
TRD_HOME=~/.trd-live trd engine cash      # capital 100.00, all settled
TRD_HOME=~/.trd-live trd engine guard     # 25 / 100 / 5 / 20
```

Want it tighter for the first week? e.g. `TRD_HOME=~/.trd-live trd engine guard
--max-loss 10`.

### 5. Deploy the live engine and the shared jobs

```bash
echo y | ./scripts/deploy-k3s.sh --skip-build --live
echo y | ./scripts/deploy-k3s.sh --skip-build --report
echo y | ./scripts/deploy-k3s.sh --skip-build --review
echo y | ./scripts/deploy-k3s.sh --skip-build --bot
```

✅ `--live` shows the red **LIVE — REAL MONEY** header and `✅ Live engine
configured`. The other three end with `live=/Users/…/.trd-live`, not "(no live
home yet)". The script refuses an unconfigured or paper home and refuses
`--test` on live — by design.

### 6. The first live morning

- 09:30 — scans start. On daily bars the engine may want nothing for days.
- Before the first order, one pass that reviews and never places:

  ```bash
  RH_DRY_RUN=1 ~/.trd-runner/bin/live-runner.sh; tail -3 ~/Library/Logs/trd-live-runner.log
  ```

- After the first scan (~09:35), add the live engine to the watchdog — not
  before, or its missing `status.json` reads as an engine that stopped:

  ```bash
  P=~/Library/LaunchAgents/io.silverbeer.trd.watchdog.plist
  sed -i '' 's#swing20=$HOME/.trd-swing20"#swing20=$HOME/.trd-swing20,live=$HOME/.trd-live"#' "$P"
  launchctl unload "$P" && launchctl load "$P"
  ```

- `/status live` in Telegram: `💵 LIVE · open intents 0 · awaiting approval 0`.

### 7. Each order

Telegram asks → you approve in the app → Telegram confirms the fill within ~10
minutes. Ignore it and it expires at 16:00; nothing happens.

---

## Turn it off

Pick the smallest step that does what you need. They are listed from "pause for
a moment" to "gone".

### Decline one order

Decline it in the broker's app. The runner never re-places a declined order;
the next read-back marks it cancelled.

### Pause the engine — the kill switch

```bash
TRD_HOME=~/.trd-live trd engine halt --reason "pausing"
TRD_HOME=~/.trd-live trd engine resume          # to undo
```

Halted: the scan writes no buys and the outbox hands the runner **nothing the
engine decided — sells included**. Exits are still decided and recorded, so the
book shows what it wants, but nothing is placed. The exceptions are a manual
close (*Close one position*, below) and anything in a **human slot** (its buys and
its exits — *Human slots*, below): the outbox still hands those over.

> ⚠ **With open positions, halting means you manage the exits.** A stop the
> engine would have sold at becomes a recorded intent nobody places. Watch the
> positions (`trd engine positions`) and close by hand with `trd engine close`,
> or use *Wind down* instead.

### Close one position

```bash
TRD_HOME=~/.trd-live trd engine close NVDA --reason "taking it off" [--rm] [--notify]
```

Writes a SELL for everything still held (rule `manual`) exactly as an exit rule
would: nothing is sold here, the position stays open until the broker fills it.
The runner places it on its next pass (pre-market: at the open), you **approve
it in the broker app**, and the fill ingest closes the position. It expires at
16:00 ET like any order; unapproved, run the command again the next day.

- Works while **halted** — the outbox hands over manual closes and human-slot
  orders, nothing else the engine decided.
  The runner must be on a build that knows this: after deploying it, re-pin
  with `deploy/live-runner.sh install`, or a halted outbox places nothing.
- An exit the engine already queued for the whole position is **adopted**: the
  same intent, relabelled manual, never a second order — the runner may already
  have placed it. One already at the broker (submitted, or waiting on your
  approval), or one selling only part of the position, refuses the close —
  approve or decline that one, or let it fill and close the rest.
- If the broker rejects the sell, the alert chat says so: run the command again.
  An unapproved close that lapses at 16:00 is alerted the same way. No scan
  re-issues a manual close.
- Refused after 16:00 ET and at weekends: the order would lapse before it could
  be placed. Pre-market is fine.
- `--rm` also drops the name from the universe, so the engine does not buy it
  back. `--notify` sends the order to the private alert chat.
- Manual closes are left out of `trd engine report`'s per-strategy scorecard —
  the exit was your call, not the strategy's. Their money still counts.

A paper engine sells at the quote immediately.

### Human slots — you pick the entry, the engine runs the exit

```bash
TRD_HOME=~/.trd-live trd engine human set --slots 2 --capital 1000   # once; deposit it at the broker
TRD_HOME=~/.trd-live trd engine human buy NVDA --note "why"         # in session only
TRD_HOME=~/.trd-live trd engine human run NVDA [--off]              # let a winner run
TRD_HOME=~/.trd-live trd engine human                               # the sleeve at a glance
```

- **The buy** is sized to one human slot (human capital / slots), with the stop
  and target the engine would set. The runner places it; you approve it in the
  app. Session hours only (09:30-16:00 ET): pre-market it would be stale by the
  open. Unplaced after 30 minutes it is withheld as stale, like any buy.
- **The exit** is the engine's: stop, trailing stop, 2R target, indicator and
  10-session time exit. `human run SYM` skips the target and the time exit; the
  stop and trail stay. `trd engine close SYM` closes it by hand.
- **Cash.** `--capital` is ADDED to the total; the engine sleeve keeps what it
  had. Each sleeve has its own T+1 ledger and its own bridge (`--bridge N`), so a
  human buy never spends the engine's cash. `trd engine cash` shows the engine
  sleeve; `trd engine human` shows the human one; `reconcile --compare-cash`
  checks the two together against the broker.
- **Guard.** The engine's caps, kill switch and loss stop ignore the human
  sleeve. The human sleeve has its own loss stop (`--max-loss`, default 20% of
  its capital): tripped, it halts human BUYS only — exits keep running —
  until `trd engine human resume`.
- **One owner per name.** The scan never buys a name in a human slot; a human
  buy of a name the engine holds is refused. Human slots never count against the
  engine's slots.
- Human trades are left out of the per-strategy scorecard. Reports do not yet
  show a separate HUMAN block — the positions appear in `trd engine positions`
  with strategy `human` (follow-up ticket).

### Wind down — no new buys, exits keep working

The caps apply to buys only, so a tiny order cap stops entries while every exit
is still placed for your approval:

```bash
TRD_HOME=~/.trd-live trd engine guard --max-order 0.01
```

Each position closes on its own rule (stop, target, trail, time). Undo with
`--max-order 25`. This is the right way to get flat.

### Stop placing orders, keep the engine deciding

```bash
launchctl unload ~/Library/LaunchAgents/io.silverbeer.trd.live-runner.plist
# again:  launchctl load ~/Library/LaunchAgents/io.silverbeer.trd.live-runner.plist
```

Intents pile up in the outbox and expire at 16:00. Unlike halting, nothing is
placed — not even a manual close. The watchdog will alarm that the runner stopped — expected.

### Stop the engine scanning

```bash
kubectl patch cronjob trd-live-scan -n trd -p '{"spec":{"suspend":true}}'
kubectl patch cronjob trd-live-queue -n trd -p '{"spec":{"suspend":true}}'
# again: the same with "suspend":false
```

No scans, no intents, no exits decided. Use it with no open positions; the
watchdog alarms on a live engine that stopped scanning — unload it from the
watchdog too (below) if this is more than a moment.

### Emergency — nothing may trade, now

1. **Decline** any approval waiting in the broker's app.
2. `TRD_HOME=~/.trd-live trd engine halt --reason "emergency"`
3. `launchctl unload ~/Library/LaunchAgents/io.silverbeer.trd.live-runner.plist`

With approvals on, step 1 alone guarantees nothing executes: no order runs
without your tap. Steps 2–3 stop new requests arriving. Open positions are then
yours to manage in the app — and any sale you make there is unknown to trd until
you bring the books back in line (see *Selling in the app* below).

`trd engine close` is for **before** you sell, never after: it queues a sell of
its own. If you have already sold in the app, do not run it — that position's
books need fixing instead (*Selling in the app*).

### Decommission — turn it off for good

1. **Get flat** with *Wind down* and wait until `TRD_HOME=~/.trd-live trd engine
   positions` is empty and `trd engine intents --status submitted` shows nothing.
2. Stop everything:

   ```bash
   launchctl unload ~/Library/LaunchAgents/io.silverbeer.trd.live-runner.plist
   kubectl delete cronjob trd-live-scan trd-live-queue -n trd
   ```

3. Take the home out of the shared jobs — move it aside, and the deploy script
   drops it from the report, review and bot:

   ```bash
   mv ~/.trd-live ~/.trd-live.retired-$(date +%F)
   echo y | ./scripts/deploy-k3s.sh --skip-build --report
   echo y | ./scripts/deploy-k3s.sh --skip-build --review
   echo y | ./scripts/deploy-k3s.sh --skip-build --bot
   ```

4. Remove `,live=$HOME/.trd-live` from the watchdog plist and reload it.
5. Optional: leave the login check running (it's harmless) or unload
   `io.silverbeer.trd.broker-auth-probe` too. Withdraw the cash in the broker's app
   once it has settled.

The retired home keeps every intent, fill and trade — `TRD_HOME=~/.trd-live.retired-…
trd engine report` still works on it.

---

## When something goes wrong

| You see | It means | Do |
|---|---|---|
| 🔴 *The broker login check FAILED* | the MCP sign-in lapsed; nothing can be placed | on the mini: `cd ~/gitrepos/trd && claude`, `/mcp` → pick the broker's server → authenticate. The all-clear follows within the hour |
| 🔴 *…FAILED* with *did not connect in time* | the broker's MCP was still `pending` after the session started and its retry, so the pass placed nothing. This is not a sign-out (SB-1508) | wait; it clears within a pass or two. Still failing after an hour: `/mcp` as above |
| ⚠️ *Today's IRA report failed* | `deploy/alloc-report.sh` exited non-zero; the report was not sent. With *behind the installed build*, a runner install brought a migration the portfolio home never got (SB-1524) | `deploy/live-runner.sh install` (it now migrates the portfolio home), then `~/.trd-runner/bin/alloc-report.sh` to resend today's |
| 🔴 *Trade approvals are OFF* | someone turned approvals off; the runner refuses to place | The broker app → Agent Settings → Safety Controls → approvals on |
| ⛔ *HALTED by the runner* | an order call didn't match any intent | **decline** anything unexpected in the app, read `~/Library/Logs/trd-live-runner.log`, then `trd engine resume` |
| ⛔ *HALTED by its loss stop* | −$20 reached (realized + unrealized) | decide deliberately; `trd engine resume` only after looking at why |
| 💵 *NOT placed — the broker's review flagged it* | the pre-trade check found a problem (e.g. buying power) | nothing; it lapses at 16:00. Recurring = look at `trd engine cash` |
| 🟠 *live runner last ran N minutes ago* | the launchd job stopped or is stuck | `launchctl list \| grep live-runner`; the log; a stale lock clears itself after 20 min |
| 🟠 *stuck order intent(s)* | the broker held an order past its session, or an approval past 16:00 | `TRD_HOME=~/.trd-live trd engine intents`; the next runner pass usually resolves it |
| ⚠ *placed but could not record it* | the order reached the broker but the ack failed | **do not re-place**; run the `outbox ack` command in the message |

### Selling in the app

trd only knows about orders it asked for. To sell one position yourself, use
`trd engine close SYMBOL` (*Close one position*) **instead of** the app: trd asks
for the sale, so the fill is matched and the books stay right.

If you have **already** sold an engine position in the broker app, do **not** run
`trd engine close` — it would queue a second sell of shares you no longer hold.
The app sale shows up in `trd engine fills` as *unmatched* (reported, never
booked), trd still thinks the position is open, and its stop and the exit rules
keep running on it. `trd sell` refuses engine-held symbols on purpose. Halt the
engine (`trd engine halt`) so nothing more is asked for, confirm the gap with
`trd engine reconcile` (the name reads `MISSING AT BROKER`), and ask for help
fixing the books: trd has no command that books a sale it did not ask for.

### Where things are

| What | Where |
|---|---|
| live engine database | `~/.trd-live/trd.duckdb` (never in iCloud) |
| runner + login-check log | `~/Library/Logs/trd-live-runner.log` (tagged `[runner]` / `[probe]`) |
| runner heartbeat | `~/.trd-live/runner.json` |
| runner's last broker snapshots | `~/.local/state/trd/runner/snapshot-*.json` (kept 14 days) |
| pinned runner code | `~/.trd-runner/` (`VERSION` = the sha) |
| runner secrets | `~/.config/trd/live.env`, chmod 600 — never commit, never `cat` |
| runner permissions | `deploy/live-runner-settings.json` |
| k3s objects | `trd-live-scan`, `trd-live-queue` in namespace `trd` |

### Useful commands

```bash
export TRD_HOME=~/.trd-live
trd engine perf            # worth session by session from the capital, vs SPY
trd engine status          # health, cash, halt, caps, intents
trd engine intents         # every order request and what became of it
trd engine positions       # open trades: entry, stop, target, R
trd engine live-report     # the 16:20 message, printed here (no --notify)
trd engine human brief     # the 16:45 brief for your own picks, printed here
trd engine outbox          # what the runner would place right now
trd engine close SYM       # sell one position by hand (you approve in the app)
trd engine cash            # settled vs unsettled, bridge, buying power
trd engine report          # per-strategy scorecard
RH_DRY_RUN=1 ~/.trd-runner/bin/live-runner.sh   # a review-only pass
```

---

## Open decisions

- **When approvals could go off.** Not before N live trades match swing20 within
  tolerance and every guard has fired in a test. N is undecided (20 suggested).
  Until then the phone tap stays.
- **Your portfolio view.** Live trades live in `~/.trd-live`, not the iCloud
  `trd.duckdb` (`paper-agent`), so they do not show in `trd portfolio` yet.
- **The idle surplus.** The engine's $100 is enforced by trd (ledger + caps), not by
  the broker: the broker would accept a sixth $20 buy.
- **How long a broker sign-in lasts.** Being measured: every hourly login
  check is in the log. Decides whether "unattended" means a weekly or a daily
  re-login.
