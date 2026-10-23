# The live engine — real money on Robinhood

The swing engine's rules, trading **$100 of real money** in the Robinhood agentic
account (••4422, a cash account): 5 slots of $20, T+1 settled cash, every order
approved by you on your phone.

This is the operator's guide: what it is, how an order travels, how to turn it
on, how to turn it off, and what to do when something goes wrong. The design
behind it is in [robinhood-mcp.md](robinhood-mcp.md) ("Draining the outbox", "The
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
| Account | `rh-live` in `~/.trd-live` = the Robinhood agentic account. The other ~$200 in that account is idle and **not** the engine's |
| Paper twin | `swing20` (`~/.trd-swing20`): identical config on paper. The daily report compares them — this is how live fills are judged |
| Human gate | Robinhood trade approvals **ON**. Nothing executes until you tap Approve |

**A live engine decides; it never trades.** Where the paper engines write a
trade, the live scan writes an **order intent** — side, quantity, the price it
saw, why. Something outside trd has to place it, and trd only learns about the
fill when the broker's records are read back.

## How an order travels

```
k3s  trd-live-scan (every 5 min, 09:30–16:00)
       └─ writes an order intent ──────────────► trd engine outbox
mini launchd  live-runner (every 10 min, 09:30–16:15)
       ├─ review_equity_order        (Robinhood's pre-trade check)
       ├─ place_equity_order         → held as a trade approval
       ├─ outbox ack --notify        → 📱 Telegram: "approve BUY X … in the Robinhood app"
you    └─ tap Approve in the Robinhood app  (or Decline, or ignore → expires 16:00)
mini  live-runner, next pass
       ├─ get_equity_orders / get_trade_approvals
       └─ trd engine fills --notify  → 📢 Telegram: "filled BUY X … @ p (scan saw r, ±s%)"
```

The runner is a headless `claude -p` on the mini, because the Robinhood MCP's
sign-in lives in Claude Code. **The model makes the calls and never reports
them**: `deploy/rh_stream.py` reads what the broker actually returned from the
session transcript, and checks every order call against the intent that asked
for it. An order call nothing asked for halts the engine.

### What you receive

| When | Where | Message |
|---|---|---|
| an order is waiting for you | private chat | `💵 LIVE: approve BUY NVDA 0.104712 sh (~$20.00 @ 191.00) in the Robinhood app — expires 16:00 ET` + the reason |
| it filled | channel | `[💵 live] filled BUY NVDA 0.104712 @ 190.88 (scan saw 191.00, -0.06%)`; a sell adds realized $ and R |
| rejected, expired, or Robinhood's review flagged it | private chat | what happened; nothing for you to do |
| Robinhood login lapsed, or approvals switched off | private chat | how to fix it; reminders ≤ every 3h in the session; an all-clear |
| the engine halted itself (loss stop) or the runner halted it | private chat | why, and what to check |
| runner stopped / orders stuck | private chat (watchdog) | `🟠 trd live engine needs a person` |
| every weekday 16:16 | channel | daily report: **💵 LIVE (real money)** block first, then paper; live vs swing20 |
| every weekday 16:20 | private chat | `💵 trd live — <date>`: the account (capital, worth, return, today's move, cash, orders, halt), then every open position — P&L, R, today's move, stop in force (↑ = trailing) and how far below, target, at risk — then the day's buys, exits and orders that never filled. `trd engine live-report`; CronJob `trd-live-report`, applied by `--live`. Nothing is sent on a market holiday |

`/status live` in the bot shows a halt first, then open intents, awaiting
approval, settled cash and stuck intents.

### The safety rails

| Rail | What it does |
|---|---|
| Trade approvals (Robinhood) | every order waits for your tap. The runner **refuses to place** if approvals are off |
| Hard caps (`trd engine guard`) | max order $25, max $100 of buys per day, 5 positions, loss stop $20 — independent of the strategy config, checked at the scan and again before placing. Buys only |
| Loss stop | realized + unrealized ≤ −$20 → the engine halts itself and tells you |
| Stale buys | a buy sized off a quote older than 30 min is withheld and re-planned |
| Kill switch | `trd engine halt` — see [Turn it off](#turn-it-off) |
| Runner checks | an order call that doesn't match its intent halts the engine |
| Watchdog | alarms if scans stop, the runner stops (25 min), or intents get stuck |
| Login check | hourly; Telegram when the Robinhood sign-in lapses |

Stops are **not** resting orders at the broker — Robinhood will not rest a stop
on fractional shares. An exit happens when a scan sees the stop crossed, writes a
sell intent, and you approve it. A gap overnight is taken at the open, as on paper.

---

## Turn it on

Do steps 1–5 with the market closed (after 16:45, or before 09:00). Step 3 pulls
two years of bars for 137 names and takes several minutes. Nothing spends money
until step 6.

**Already in place** (2026-10-22): trade approvals ON, $300 funded, launchd jobs
`io.silverbeer.trd.live-runner` and `io.silverbeer.trd.rh-auth-probe` loaded,
`~/.config/trd/live.env` written. Check with:

```bash
launchctl list | grep -E 'trd\.(live-runner|rh-auth-probe)'   # both listed, status 0
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
TRD_HOME=~/.trd-live trd engine init --live --account rh-live \
    --capital 100 --size 20 --max 5 --symbols "$SYMS" --strategies "$STRATS"
TRD_HOME=~/.trd-live trd sync --full
```

`engine init --live` creates the `rh-live` real account. If swing20 runs with a
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

Decline it in the Robinhood app. The runner never re-places a declined order;
the next read-back marks it cancelled.

### Pause the engine — the kill switch

```bash
TRD_HOME=~/.trd-live trd engine halt --reason "pausing"
TRD_HOME=~/.trd-live trd engine resume          # to undo
```

Halted: the scan writes no buys and the outbox hands the runner **nothing —
sells included**. Exits are still decided and recorded, so the book shows what
it wants, but nothing is placed.

> ⚠ **With open positions, halting means you manage the exits.** A stop the
> engine would have sold at becomes a recorded intent nobody places. Watch the
> positions (`trd engine positions`) or use *Wind down* instead.

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

Intents pile up in the outbox and expire at 16:00. Same caveat as halting:
exits are not placed. The watchdog will alarm that the runner stopped — expected.

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

1. **Decline** any approval waiting in the Robinhood app.
2. `TRD_HOME=~/.trd-live trd engine halt --reason "emergency"`
3. `launchctl unload ~/Library/LaunchAgents/io.silverbeer.trd.live-runner.plist`

With approvals on, step 1 alone guarantees nothing executes: no order runs
without your tap. Steps 2–3 stop new requests arriving. Open positions are then
yours to manage in the app — and any sale you make there is unknown to trd until
you bring the books back in line (see *Selling in the app* below).

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
   `io.silverbeer.trd.rh-auth-probe` too. Withdraw the cash in the Robinhood app
   once it has settled.

The retired home keeps every intent, fill and trade — `TRD_HOME=~/.trd-live.retired-…
trd engine report` still works on it.

---

## When something goes wrong

| You see | It means | Do |
|---|---|---|
| 🔴 *Robinhood login check FAILED* | the MCP sign-in lapsed; nothing can be placed | on the mini: `cd ~/gitrepos/trd && claude`, `/mcp` → robinhood-trading → authenticate. The all-clear follows within the hour |
| 🔴 *Trade approvals are OFF* | someone turned approvals off; the runner refuses to place | Robinhood app → Agent Settings → Safety Controls → approvals on |
| ⛔ *HALTED by the runner* | an order call didn't match any intent | **decline** anything unexpected in the app, read `~/Library/Logs/trd-live-runner.log`, then `trd engine resume` |
| ⛔ *HALTED by its loss stop* | −$20 reached (realized + unrealized) | decide deliberately; `trd engine resume` only after looking at why |
| 💵 *NOT placed — Robinhood's review flagged it* | the pre-trade check found a problem (e.g. buying power) | nothing; it lapses at 16:00. Recurring = look at `trd engine cash` |
| 🟠 *live runner last ran N minutes ago* | the launchd job stopped or is stuck | `launchctl list \| grep live-runner`; the log; a stale lock clears itself after 20 min |
| 🟠 *stuck order intent(s)* | the broker held an order past its session, or an approval past 16:00 | `TRD_HOME=~/.trd-live trd engine intents`; the next runner pass usually resolves it |
| ⚠ *placed but could not record it* | the order reached Robinhood but the ack failed | **do not re-place**; run the `outbox ack` command in the message |

### Selling in the app

trd only knows about orders it asked for. If you sell an engine position by hand,
the fill shows up as *unmatched* and trd still thinks the position is open. `trd
sell` refuses engine-held symbols on purpose. Prefer letting the engine exit
(*Wind down*), or halt first and ask for help reconciling.

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
trd engine status          # health, cash, halt, caps, intents
trd engine intents         # every order request and what became of it
trd engine positions       # open trades: entry, stop, target, R
trd engine live-report     # the 16:20 message, printed here (no --notify)
trd engine outbox          # what the runner would place right now
trd engine cash            # settled vs unsettled
trd engine report          # per-strategy scorecard
RH_DRY_RUN=1 ~/.trd-runner/bin/live-runner.sh   # a review-only pass
```

---

## Open decisions

- **When approvals could go off.** Not before N live trades match swing20 within
  tolerance and every guard has fired in a test. N is undecided (20 suggested).
  Until then the phone tap stays.
- **Your portfolio view.** Live trades live in `~/.trd-live`, not the iCloud
  `trd.duckdb` (`rh-agent`), so they do not show in `trd portfolio` yet.
- **The idle $200.** The engine's $100 is enforced by trd (ledger + caps), not by
  Robinhood: the broker would accept a sixth $20 buy.
- **How long a Robinhood sign-in lasts.** Being measured: every hourly login
  check is in the log. Decides whether "unattended" means a weekly or a daily
  re-login.
