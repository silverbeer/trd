# trd — Investment Tracker

Local-first investment tracker. Full architecture and roadmap: [DESIGN.md](DESIGN.md).

## Commands

```bash
uv sync                     # install deps
uv run trd --help           # CLI entry point
uv run pytest               # tests (no network — FakeProvider in tests/conftest.py)
uv run ruff check . && uv run ruff format --check .
uv run ty check             # type checking
```

## CLI quick reference

```bash
trd init                              # create ~/.trd/trd.duckdb + 'main' account
trd account add ira                   # one account per brokerage (--type simulation for paper)
trd account ls
trd sync [--full] [--require-current] [--json]
                                      # refresh quotes + daily bars + earnings (--full = 2y backfill)
                                      # also pulls intraday bars for an intraday engine's universe,
                                      # driven by its config — no flag to forget
                                      # reports 'stale_symbols': names left behind the newest bar this
                                      # sync stored. An empty provider frame raises nothing and writes
                                      # nothing, so a symbol that loses the 09:30 race to publication
                                      # keeps yesterday's close all session and every mark drawn from
                                      # it (unrealized, risk at stop, R) is quietly wrong.
                                      # --require-current exits non-zero when that list is non-empty,
                                      # so an unattended runner retries instead of stamping the day done
                                      # --require-settled (AFTER the close): also exit non-zero when a
                                      # bar dated today differs from the closing quote by > 0.2% —
                                      # the morning bar is always 'current', never the close (SB-1411)
trd portfolio [--account NAME]        # holdings with live P&L
trd equity [--account NAME] [--days N | --months N] [--all] [--json]
                                      # equity curve: portfolio value over time, period return,
                                      # XIRR, max drawdown. Derived from txn FIFO × price_daily
                                      # (no snapshots); depth bounded by sync history
trd lots [SYMBOL] [--account NAME]    # per-purchase detail: buy date, paid/share, total cost, gain
trd history [--days 30] [--all-time] [--symbol X] [--side buy|sell] [--account N] [--all]
                                      # what you bought and sold, newest first, with realized P&L on
                                      # every sell (FIFO-matched) and a period total. Real money only
                                      # unless --all. FIFO matches over ALL history, then the window
                                      # filters what's shown — never the other way round
trd quote AAPL                        # live quote + profile: sector / industry / country / summary
trd buy AAPL 10 [--price 213.50] [--account main] [--date 2026-06-10] [--fees 1] [--note ...]
trd sell AAPL 5 [--price ...]         # validates held quantity
trd import txns.csv                   # bulk-load transactions
trd import activity ACTIVITY.csv --account managed-ira [--create "Managed IRA"]
              [--positions POSITIONS.csv] [--opening-from-positions] [--opening-price SYM=P]
              [--source-account NAME|NUMBER] [--core SYMBOL] [--dry-run] [--json]
                                      # the custodian's Activity History export (no retail API,
                                      # SB-1418). The file names NO account: --account is
                                      # mandatory, --create makes it (real). YOU BOUGHT /
                                      # REINVESTMENT = buy (price from the Amount, not the
                                      # cents-rounded Price), YOU SOLD = sell, DIVIDEND RECEIVED
                                      # = income dividend, LONG/SHORT-TERM CAP GAIN = income
                                      # capital_gain. A DRIP is its two rows: income in, buy out.
                                      # Core money market (tickers from TRD_HOME/core-symbols.txt,
                                      # TRD_CORE_SYMBOLS or --core; refused with none) is
                                      # CASH: its dividend = income cash_sweep, its reinvest/buy/
                                      # sell write nothing. ADVISORY FEE = cash_flow 'fee'; every
                                      # run (dry or not) lists each fee + the total. CONTRIBUTION/
                                      # ROLLOVER/TRANSFER/JOURNALED/EFT = deposit|withdrawal by
                                      # sign. Any other action refuses the WHOLE file, lines
                                      # listed. Idempotent: rows keyed in import_row (migration
                                      # 032; date|class|symbol|qty|amount|occurrence), so the same
                                      # or an overlapping file writes nothing twice; one
                                      # transaction. Sells of shares trd never saw are refused:
                                      # --opening-from-positions (first import only) seeds what was
                                      # held the day before the first row = Positions minus the
                                      # activity, at the provider's close (or --opening-price), +
                                      # one deposit of that value plus the cash before the first
                                      # row (from the file's own Cash Balance column).
                                      # --positions reconciles every quantity (0.001 sh) and the
                                      # cash (core ** rows + Pending Activity); mismatch = exit 1.
                                      # The POSITIONS PARSER IS UNVERIFIED against a real export
                                      # (says so in its output) — check it on the first one
trd income add 2.50 --symbol VOO [--account brokerage] [--date ISO] [--kind dividend|interest|cash_sweep|capital_gain]
trd income ls [--account N] [--json]  # cash a holding PAID you, as opposed to the price rising.
                                      # Not a buy: creates no shares, consumes none, never reaches
                                      # FIFO — which is why it is its own table and not a third
                                      # Side. It does reach every return that measures MONEY: XIRR
                                      # on dashboard, equity and a DCA plan. A plan's share of a
                                      # payment is split by how much of that holding the plan
                                      # actually bought, on the day it was paid, so an account that
                                      # holds SPY from old one-offs AND a contribution doesn't
                                      # credit the plan with all of it. A REINVESTED dividend
                                      # (DRIP) is an ordinary 'trd buy' — recorded here too it is
                                      # counted twice. 'trd learn income · total-return'
trd cash deposit 100000 --account ira [--date ISO] [--note ...]
trd cash withdraw AMOUNT --account N | trd cash ls --account N [--json]
                                      # money moved into/out of an account (migration 029). Not a
                                      # trade: no lot. cash = deposits - withdrawals - fees - buys
                                      # + sells + income. ls labels kind 'fee' "advisory fee". Only the alloc views read it so far; portfolio /
                                      # equity / dashboard returns are still trade-based
trd alloc set --account ira --capital 100000 --name ... [--kind "IRA"]
              [--brokerage NAME] -t IVV=45 -t IXUS=20 -t QQQM=25 -t SMH=5 -t CASH=5 [--drift 5]
                                      # --pace SPCX=24 [--pace-start ISO] (set or edit): a PACED
                                      # target (SB-1388, migration 030) — its weight of capital is
                                      # earmarked and bought in equal monthly instalments, outside
                                      # the tranches. Unbought earmark = reserved cash (part of
                                      # cash, never added to it); out of the tranche fund share,
                                      # milestone progress and the drift sleeve. propose lists an
                                      # instalment due as 'buy alongside'; show/report track it
                                      # --cash-fund TICKER --cash-yield 3.51 [--cash-yield-as-of ISO]
                                      # (set or edit; as-of defaults to today; edit --cash-yield N
                                      # alone re-rates the same fund; --no-cash-yield clears it,
                                      # migration 031): what the uninvested cash earns. show/report
                                      # print cash x yield / 12 a month and 'earned so far' = income
                                      # of kind interest|cash_sweep; a rate over 35 days old says so
trd alloc schedule --start ISO --initial 60000 --amount 10000 --every 3 --count 4 [--replace]
trd alloc show [--json]               # staged allocation plan (SB-1383): worth = holdings + cash,
                                      # gain vs net deposits (simple, not annualised; XIRR after 30
                                      # days), same deposits in SPY, drift on the INVESTED sleeve
                                      # vs targets renormalised without CASH (cash waiting by design
                                      # is not "underweight everything"), schedule with each
                                      # milestone's state DERIVED from recorded buys against its
                                      # fund share (a 5% CASH target stays cash). Missing price =
                                      # unknown, never zero. The schedule must add up to capital
trd alloc propose [--amount X | --milestone N] [--mode underweight|target] [--set SYM=AMT]
                                      # a suggested split, never an order; records nothing.
                                      # underweight: fill the gaps below (sleeve + D) x weight,
                                      # overweight funds get nothing; leftover split by weight
trd alloc edit|history · trd alloc milestone postpone|cancel|resume|note N
trd alloc report [--notify] [--json]  # the daily performance message (SB-1384, SB-1416), Telegram
                                      # HTML: verdict on the day (GREAT/GOOD/FLAT/DOWN/BAD vs prior
                                      # close; flat < 0.25%, big >= 1.5%) and the funds vs the S&P
                                      # 500 today; worth + since funding (simple; XIRR after 30d);
                                      # a scoreboard — You (all) on DEPOSITS, You (invested) on net
                                      # cost bought, then SPY/QQQ/VT/AOR and the plan's own target
                                      # mix, each bought with the SAME invested dollars on the SAME
                                      # days (services/alloc_report.py; one same-days core in
                                      # services/benchmark.py). Closes: stored bars, else provider,
                                      # never stored; no price = '—'. Funds best since-bought first.
                                      # 1W/1M only in --json, once the account is that old.
                                      # --json = {message, sent, view, report}. READ-ONLY connection
                                      # and no quote snapshots — safe unattended on the iCloud DB.
                                      # Terminal prints the message with tags stripped. --notify = the
                                      # PRIVATE alert chat, never the channel. launchd weekdays 16:25:
                                      # deploy/alloc-report.sh + io.silverbeer.trd.alloc-report.plist
trd backup data.json                 # export user-owned facts (txns/accounts/plans/watch/indicators)
                                      # + the point-in-time earnings archive, which sync CANNOT rebuild
trd restore data.json [--force]      # rebuild a DB from a backup, then trd sync (cross-machine sync)
trd watch add NVDA [--list ai]        # follow a symbol (creates list if needed)
trd watch rm NVDA [--list ai]
trd watch ls [ai]                     # quote board: price, day Δ%, 52w pos, vol/avg, next earnings
trd earnings [--days 14]              # upcoming earnings across everything tracked
trd exit set ABBV --account paper-agent --stop 221 [--target 244.81] [--note ...]
                                      # stop/target exit trigger on a holding (one per account+symbol)
trd exit ls|check|rm [--account NAME]  # ls = all triggers vs latest close; check = only breaches
                                      # rule fires on a daily CLOSE beyond the level (run trd sync)
trd indicators NVDA                   # indicator panel with plain-English readings
trd indicator ls|catalog|add|rm|info  # manage followed indicators (trd indicator add ema -p period=8)
trd dca set --account dca --monthly 100 --day 15 --alloc SPY=40 --alloc QQQ=40 --alloc SMH=10 --alloc ARKX=10
                                      # DCA plan on ANY account; real accounts: you execute at
                                      # the broker, trd records + scores vs SPY ('trd plan' = alias)
trd dca invest [--account NAME] [--date 2026-01-15]    # record the month (once/month/plan)
trd dca show [--account NAME]         # flagship view: XIRR, per-symbol drift, cadence/streak
trd dca history [--limit N]           # every contribution event with legs and prices
trd dca forecast [--years 10] [--seed N]   # CAGR projection + Monte Carlo p10/p50/p90 bands
trd dca backtest [--years 10]         # replay the exact plan against real (adjusted) history
trd dca status|ls|edit|pause|resume   # quick view, list, partial update, lifecycle
                                      # edit --alloc IVV=25 --alloc IXUS=25 ... re-targets the plan
                                      # (weights sum to 100); recorded buys keep their symbols,
                                      # only future contributions follow the new split
trd sunday-prep [--json] [--snapshot] [--date ISO]   # alias 'trd prep'
                                      # week-ahead briefing: futures, macro calendar, curated-universe
                                      # earnings, sector leadership, VIX, SPY/QQQ/IWM levels, themes,
                                      # watchlist, risks. Deterministic narrative; --snapshot writes
                                      # TRD_HOME/prep/<date>.{json,md} (the scheduled mini job uses this)
                                      # --notify: one Telegram message (HTML, phone-first) to the PRIVATE
                                      # alert chat — the Sunday 18:05 job sends it (deploy/sunday-prep.sh)
trd learn [TERM]                      # investing dictionary: every term + exact formula trd uses
                                      # incl. engine terms (r-multiple, expectancy, survivorship);
                                      # indicator/strategy/exit entries generate from the registries
trd sync --years 10                   # deep backfill (forecast/backtest need long history)
trd sim init --monthly 100 [--strategy ticker|momentum] [--ticker SPY] [--alloc ...] [--name NAME]
                                      # sim = plan on a paper (simulation) account; sim invest/status same
trd engine init [--account NAME] [--size 1000] [--max 5] [--symbols A,B,...] [--strategies K,K]
                      [--sizing exposure|risk] [--timeframe 1d|5m|15m|30m|1h] [--flat-at 1555]
                      [--regime-sma 100] [--regime-vix-max 30] [--capital C] [--live]
                                                 # 'exposure' commits --size/trade (risk floats);
                                                 # 'risk' risks --size/trade (position size floats)
                                                 # --timeframe is the bar width the rules run on;
                                                 # a day engine (--flat-at) is refused on 1d bars,
                                                 # where a 2xATR stop can't be reached before the bell
                                                 # --regime-* gate NEW ENTRIES on the market, not the
                                                 # name: no buys while SPY is under its N-day or VIX
                                                 # is over V. Exits keep running. BOTH OFF BY DEFAULT
                                                 # ('trd engine status' shows the gate either way)
                                      # monitor-mode trading engine: paper-trades a 10-name universe
                                      # on a simulation account. Needs 'trd sync --full' (200 bars)
                                      # --live: trade a REAL account instead. Scans fill nothing;
                                      # each entry/exit is written as an ORDER INTENT ('trd engine
                                      # intents') for the agent-side broker session to place.
                                      # Refused without --capital (cash account, T+1 ledger is
                                      # mandatory), on intraday bars, or with --flat-at; refused on
                                      # a simulation account, as paper is refused on a real one.
                                      # An open buy intent holds a slot, the name, its entry-budget
                                      # place and its notional in settled cash; an open sell intent
                                      # leaves the position open and blocks a second exit for it.
                                      # 'engine trim' is refused live. Fills are ingested from a
                                      # broker snapshot ('trd engine fills'); an agent session
                                      # places intents from 'trd engine outbox'
trd engine scan [--paper/--no-paper] [--json] [--exits-only]
                                      # one pass: exits first, then best-ranked entries.
                                      # --exits-only manages the book and ranks but buys nothing:
                                      # the entrypoint passes it when a scan is FORCED outside the
                                      # session (a deploy check). On Sunday 2026-08-16 a forced
                                      # scan opened five day positions on Friday's quotes and
                                      # carried them into a -3R Monday gap past a stop that had no
                                      # scan to fire in. A forced scan proves the deploy; it never
                                      # takes a position
trd engine monitor [--interval 60] [--passes N] # live view on a terminal: book stays still, clock/
                                      # capacity/activity move. Piped or --ndjson falls back to scrolling
trd engine intents [--status S] [--json]
                                      # a live engine's order intents, newest first: side, qty,
                                      # ref price (what the scan saw — never a fill), notional,
                                      # status (pending | submitted | awaiting_approval | filled |
                                      # rejected | cancelled | expired; the first three are open)
                                      # and the fill when one lands. Empty on a paper engine.
                                      # 'trd learn order-intent'
trd engine fills SNAPSHOT.json [--notify] [--json]
                                      # ingest a LIVE engine's broker fills (refused on paper).
                                      # SNAPSHOT is the broker's own reads written as-is, no
                                      # reshaping: {"orders": [get_equity_orders rows],
                                      # "approvals": [get_trade_approvals rows], "taken_at"}.
                                      # An order matches an intent when its ref_id is the intent's
                                      # ref_id OR its approval_id — with trade approvals on the
                                      # broker stamps the order with the APPROVAL's id, not ours
                                      # (measured 2026-10-21) — or its id is the broker_order_id
                                      # already recorded. Symbol/side disagreeing = CONFLICT, never
                                      # applied, exit non-zero. filled (or partial-then-cancelled)
                                      # writes the txn at the broker's price/fees/time (UTC → ET):
                                      # a buy opens the position at the FILL with the stop and
                                      # target re-derived from atr_at_entry, so live R is measured
                                      # from what was paid; a sell closes with the intent's rule,
                                      # or trims when less than the remainder sold. queued/
                                      # confirmed/partially_filled → submitted; rejected/failed →
                                      # rejected; cancelled → cancelled; approval DECLINED →
                                      # cancelled, EXPIRED → expired, PENDING → awaiting_approval.
                                      # Unmatched orders/approvals are REPORTED, never adopted.
                                      # Idempotent: keyed on intent status + broker_order_id, so
                                      # re-ingesting a snapshot writes nothing. Then sweeps:
                                      # pending/awaiting_approval from an earlier session, or
                                      # today's once it is 16:00 ET (approvals lapse at the close),
                                      # → expired. Submitted never expires. A live scan runs the
                                      # same sweep first, so a stale buy stops holding its slot.
                                      # 'trd sell'/'trd buy' on a name with an open intent is refused
                                      # --notify: each booked fill to the channel ("[💵 live] filled
                                      # BUY X q @ p (scan saw r, ±s%)", realized P&L + R on a sell);
                                      # rejected / expired / conflicts to the live ALERT chat
trd engine outbox [--include-stale] [--json]
                                      # a LIVE engine's PENDING intents as order specs an agent
                                      # passes straight to place_equity_order: intent_id, ref_id,
                                      # symbol, side, quantity (6dp string, rounded down), type
                                      # market, time_in_force gfd, market_hours regular_hours, plus
                                      # ref_price, notional, reason, created_at, expires_at (16:00
                                      # ET of its session). Oldest first; already-expired ones left
                                      # out (not swept — read-only connection). Written at/after
                                      # 15:45 ET = "late": true + a note: minutes left to approve.
                                      # Refused on paper. Runbook: docs/broker-mcp.md
                                      # "Draining the outbox"
                                      # GUARDRAILS (SB-1377), applied again here against the caps
                                      # as they stand NOW: halted = "halted": true, halt_reason,
                                      # orders EMPTY (sells too). A buy that breaks a hard cap, or
                                      # one older than STALE_INTENT_MINUTES (30) — "stale — sized
                                      # off a quote from HH:MM" — goes to "withheld" with its
                                      # reason, never "orders". --include-stale lists stale buys
                                      # in orders (flagged in note, still capped). Sells are never
                                      # withheld by a cap or as stale: an exit nobody places sits
                                      # unmanaged until 16:00, since nothing re-plans it
trd engine outbox ack INTENT (--approval-id X | --broker-order-id Y | --error MSG) [--notify] [--json]
                                      # the agent's write-back after placing; INTENT = id or ref_id.
                                      # approval: pending → awaiting_approval; order: pending/
                                      # awaiting_approval → submitted; error: pending → rejected.
                                      # Same ack twice = no-op (changed: false). A terminal intent,
                                      # or a different approval/order id than the one recorded =
                                      # IntentAckError, exit 1. Never mints a ref_id: a retry reuses
                                      # the intent's. Same transition helpers as 'engine fills'.
                                      # --notify with --approval-id: "approve … in your broker
                                      # app" to the ALERT chat = TRD_LIVE_ALERT_CHAT_ID, else first
                                      # TRD_BOT_ALLOWED_USER_IDS id, else REFUSED with an error —
                                      # a call to action on real money never falls back to the
                                      # public channel (SB-1377). Fill reports still go there.
                                      # A live scan's intents go to that chat too, not the channel —
                                      # only with TRD_LIVE_INTENT_ALERTS=1: with the live runner
                                      # (SB-1382) the approval request is the one message per order
trd engine halt [--reason TEXT] [--json]
trd engine resume [--json]            # a LIVE engine's kill switch. Halted: the scan writes no buy
                                      # intents (sells still recorded, so the book shows what it
                                      # wants; signals still logged) and the outbox emits nothing.
                                      # Stored in engine_guard (migration 028), keyed by account,
                                      # so it survives 'engine cash --set', a re-init and
                                      # backup/restore (v7). Paper refused. 'trd learn kill-switch'
trd engine guard [--max-order D] [--max-daily D] [--max-positions N] [--max-loss D] [--json]
                                      # a LIVE engine's hard caps, independent of the strategy
                                      # config; no flags = show. Defaults at init --live (or, for a
                                      # live engine with no stored row, derived on read):
                                      # max-order = size x 1.25, max-daily = capital, max-positions
                                      # = --max, max-loss = 20% of capital. Enforced twice: the
                                      # scan writes no intent that breaks one (why in skipped), the
                                      # outbox withholds a pending one that breaks one now. Buys
                                      # only. max-daily = notional of today's buys not rejected/
                                      # cancelled/expired (filled at the fill price). Loss stop:
                                      # realized + unrealized <= -max-loss AUTO-HALTS (halted_by
                                      # max_loss), checked on every live scan and fill ingest;
                                      # --notify alerts the ALERT chat once. A cap cannot be set to
                                      # 0 (off) — set it as high as you mean it
trd engine positions [--all]          # open trades: entry, stop (↑ = trailing in force), risk, target, R
                                      # risk = (mark − stop in force) × remaining qty, floored at 0 —
                                      # what this trade loses from here, not what it committed
trd engine add SYMBOL                 # add a name to THIS engine's universe and pull only its
                                      # history (2y daily, plus intraday on a day engine). Says
                                      # whether it clears the engine's warmup — a name with no
                                      # bars is skipped every pass and reads as a broken engine
                                      # rather than one warming up. Same code path as the bot's
                                      # /add, so chat and terminal cannot drift
trd engine rm SYMBOL                  # drop a name from the universe. An OPEN POSITION in it is
                                      # deliberately left alone: no new entries, but the trade on
                                      # keeps its stop/target and closes on its own exit rules
trd engine trim SYM --pct 50          # sell part of an open position, leave the rest running.
                      [--quantity N] [--price P]   # for taking cash out without abandoning the
                                      # trade. Stop/target/trail untouched — trimming changes the
                                      # size, not the plan. Trimming ALL of it is refused: closing
                                      # goes through an exit rule so the trade records why it ended.
                                      # R stays honest — 90% at +2R then -1R on the rest = +1.7R,
                                      # measured against the size taken at entry.
                                      # NOTE 'trd sell' on an engine-held symbol is refused; it
                                      # would desync engine_position and take the account short
trd engine signals [-n 25] [-s KEY]   # every signal fired, taken or passed over, with its reason
trd engine report                     # per-strategy scorecard: win%, avg win/loss, expectancy in R
trd engine edge [--samples K] [--seed N] [--json]
                                      # do the entries beat CHANCE? Every taken signal beside K
                                      # random entries in the same name and session (only bars
                                      # the engine could have entered on), each priced with
                                      # plan_entry's stop and walked by signal_outcome itself.
                                      # Paired per signal; edge ± SE per strategy and before/after
                                      # 09:40. Read net (MFE + MAE) first: bigger MFE AND worse
                                      # MAE than random is volatility found, not direction.
                                      # MEASURED 2026-10-16, day engine, 891 taken: net -0.01R
                                      # ±0.06 — no entry edge. Read-only. 'trd learn entry-edge'
trd engine rules                      # what each entry strategy looks for + the 5 exit rules
trd engine why SYMBOL                 # why THIS trade was taken: the numbers the rule saw at entry,
                                      # what each indicator means, and which exit is in force
trd engine runs [-n N] [--today]      # scan history + interval between passes; ⚠ marks missing scans
trd engine cash [--set AMOUNT | --off] [--json]
                                      # T+1 settled-cash ledger for a cash account: capital,
                                      # settled (buying power), unsettled sale proceeds and when
                                      # each settles. With a capital set, scan and backtest skip
                                      # an entry settled cash can't pay for — a same-pass refill
                                      # off an exit's proceeds is what a broker rejects. Off
                                      # (NULL) by default. --set/--off touch ONLY the capital;
                                      # 'engine init --capital' sets it at creation.
                                      # 'trd learn settlement'
                                      # --set refuses a capital below what the account's history
                                      # already spent: the ledger replays every fill from it, so
                                      # a cash-account book wants a fresh engine home
trd engine status [--json]            # what this engine is + whether it's healthy: build, DB, rule set,
                                      # capacity, bar depth, last scan. No network — answers when yfinance doesn't
                                      # also: realized / unrealized / NET (all three, never the total alone)
                                      # names short of a rule's warm-up are listed PER RULE (SB-1433):
                                      # 'CBRS(113): breakout active · momentum need 201'. Entries gate
                                      # per strategy, so only a name no rule clears is "not tradable
                                      # yet". 'short_detail' in --json; 'short_history' unchanged
                                      # and money at risk — what's lost if every stop hits, typically a
                                      # tenth of 'committed'. See 'trd learn risk-at-stop'
                                      # warns in red when the engine's own config is one 'init' would
                                      # refuse (a day engine on 1d bars). init can't catch that — the
                                      # engine already exists — and it reads as a flat strategy, not a
                                      # broken one. 'config_refused' in --json
                                      # also flags a configured strategy that has fired NO signal
                                      # (taken or passed) in 10 scanned sessions: a rule that
                                      # cannot fire reads as a quiet market everywhere else.
                                      # pullback sat silent 45 sessions on the day engine
                                      # (SB-1341). 'silent_strategies' in --json
                                      # live: HALTED panel at the top when the kill switch is on;
                                      # halted / halt_reason / halted_at / halted_by, guard (caps),
                                      # awaiting_approval and stuck_intents in --json — published
                                      # in status.json, which is what the watchdog reads
trd engine outcomes [--backfill] [--recompute] [--trades] [--json]
                                      # did we enter early, did we exit early, and what did the
                                      # signals we PASSED OVER do next. MAE (heat taken), MFE
                                      # On intraday bars the bar the exit landed in is AFTER the
                                      # trade, not inside it — an exit at 12:00:14 does not live
                                      # through the 12:00 bar's low, and counting it scored a
                                      # -1.8R stop-out as -7.5R. The exit price itself is folded
                                      # in as a point on the path (SB-1030). Daily bars are lived
                                      # through whole: the exit is at the bell
                                      # (best offered), capture (booked R / offered R, POOLED —
                                      # averaging ratios produced -507% on the live book and it
                                      # was an artefact), follow-through (5 sessions, or 60min of
                                      # bars intraday). All in R. Stored per position/signal
                                      # (migration 022), --backfill is idempotent
                                      # The signals table is the uncomfortable one: every unacted
                                      # signal walked forward against the stop plan_entry WOULD
                                      # have set, with the ones that fired on a full book counted
                                      # separately — they could not have been taken. Read '2R
                                      # first' vs '1R stop first', never the average return
                                      # Caveats (simulation fills, survivorship, capacity) are
                                      # printed with the numbers, not left in a doc
                                      # The last scan of the day runs --backfill itself. A row is
                                      # skipped only once FINAL (its follow-through horizon filled
                                      # in); a trade closed today is re-measured for the next five
                                      # sessions, then never again — measuring once at 10:05 would
                                      # freeze 'nothing happened after the exit' forever
                                      # 'trd learn mae|mfe|capture|follow-through|passed-signals'
trd engine review-pack [--date ISO] [--engines ...] [--window 30] [--json]
                                      # one document per session: every closed trade with its
                                      # SB-994 outcome, every signal fired (taken AND passed) with
                                      # the reason recorded at the time, each rule QUOTED with its
                                      # own stated intent, the config in force, and the day's P&L
                                      # from the same code the Telegram report reads.
                                      # A document, not a set of queries: an agent is only
                                      # reproducible if its input is
trd engine review [--date ISO] [--engines ...] [--window 30] [--snapshot] [--ai] [--model M] [--notify] [--json]
                                      # every closed trade first, in two plain sentences: what the
                                      # BUY found (graded on MFE — the move the rule was hunting
                                      # either showed up or did not) and what the EXIT kept (graded
                                      # on capture and follow-through). Independent on purpose, so
                                      # 'good buy, bad exit' is sayable. They DESCRIBE a price path
                                      # and never rule on a decision — a trade can follow every rule
                                      # and lose. 'trd learn trade-verdict'
                                      # then findings over the pack — statistics, no judgement, no
                                      # LLM. Scoped to a RULE / STRATEGY / FILTER, never a trade; each
                                      # carries n, its evidence, and the backtest that would settle
                                      # it. Under 20 trades = HYPOTHESIS (labelled); under 5 = not
                                      # reported. 'Nothing conclusive' is a real and expected
                                      # answer — a reviewer that finds something daily fits noise.
                                      # Detectors: low capture, exits price runs past, peak-early
                                      # holds, a stop no winner uses, and taken-vs-takeable-passed
                                      # signal quality (the filter's actual edge, n = the SMALLER
                                      # side — 2,000 passed signals must not dress up a conclusion
                                      # resting on thirty taken ones)
                                      # READ-ONLY by connection, not by promise; --snapshot is the
                                      # one writing step (migration 023, per engine, dated, so a
                                      # claim made Tuesday can be scored on Friday)
                                      # --ai adds a model's read ON TOP of the arithmetic, never
                                      # instead of it: the detectors run first and are handed over
                                      # as input, so a provider outage costs the commentary and not
                                      # the review. Needs the optional extra (uv sync --extra ai);
                                      # without it the deterministic review still prints.
                                      # Model: --model, else TRD_AI_MODEL, else anthropic:claude-
                                      # opus-5. Any pydantic-ai 'provider:model' passes through;
                                      # trd adds 'claude-code:<opus|sonnet>' (the local claude
                                      # binary on the subscription — no key, no per-token bill,
                                      # nothing to install: the DEV LOOP) and 'vertex:<model>'
                                      # (--extra vertex, gcloud ADC, TRD_AI_VERTEX_PROJECT). Key
                                      # from ANTHROPIC_API_KEY (laptop: op read from the agents
                                      # vault; cluster: same secret pattern as Telegram).
                                      # MEASURED 2026-09-06 on the stored 2026-09-03 pack: $1.08-
                                      # 1.38/run before (200-260k input, 8-10 requests, nothing
                                      # cached) → ~$0.20/run after (4-5 requests, ~85% of input
                                      # read from cache, tool rows trimmed to what a judgement
                                      # uses), same findings. The cache split is printed on every
                                      # run: a zero cache read means the prefix is varying and
                                      # the caching is theatre. Turns of tool use are capped at
                                      # MAX_REQUESTS; past it a call is refused (not withdrawn —
                                      # withdrawing tools invalidates the cache) and the model
                                      # answers with what it has.
                                      # Cost per run is reported only when TRD_AI_PRICE_IN/_OUT are
                                      # set — a price table in source goes stale silently and would
                                      # be wrong in the flattering direction. Cache lanes default
                                      # to Anthropic's ratios (read 0.1x, write 1.25x); override
                                      # per MTok with TRD_AI_PRICE_CACHE_READ/_WRITE
                                      # --notify sends ONE Telegram message: the arithmetic, then
                                      # the model's read below it. A date no engine has a bar for
                                      # sends nothing (a holiday must never post "nothing
                                      # conclusive"). Nightly: k3s/trd-engine/review-cronjob.yaml
                                      # (./scripts/deploy-k3s.sh --review), 16:31 ET, both engines,
                                      # --ai --snapshot --notify; key in secret trd-engine-ai
trd engine backtest [--years N] [--fill intrabar|close] [--no-blackout] [--symbols A,B]
                                      # --fill defaults to intrabar on daily bars and CLOSE on
                                      # intraday ones. MEASURED 2026-09-08 (SB-1030): the day
                                      # engine's 105 live stops averaged -1.34R, 24% worse than
                                      # -1.5R; the close replay gave -1.32R / 21%, the intrabar
                                      # replay -1.02R / 2%. A stop is honoured at the next scan,
                                      # not at the level, and intrabar on 5m bars is a fill the
                                      # day engine has never received. The result records which
trd engine backtest --scale-out N      # replay with the runner on and off. MEASURED 2026-09-05 over
                                      # 8y / 62 names / ~3,000 trades: expectancy 0.112R flat vs
                                      # 0.117R at 50% and 0.116R at 70% — noise — while max drawdown
                                      # went -31.5% → -39.5% → -42.1%. Same return, materially worse
                                      # drawdown, so scale_out_pct stays 0. Re-measure before
                                      # switching it on; do not switch it on because it sounds right
trd engine backtest -p KEY=VALUE [-p ...]   # override any exit param for one run — the exit sweep
                                      # (SB-1033): stop_atr_mult, trail_atr_mult, trail_activate_r,
                                      # max_sessions, target_r, entry_delay_minutes ... one axis at a
                                      # time against the same history. Unknown keys are refused (a
                                      # typo would replay the baseline and call it a result). Every
                                      # run now reports pooled CAPTURE and per-trade mae_r/mfe_r,
                                      # the same walk 'trd engine outcomes' does on live trades.
                                      # trail_activate_r and entry_delay_minutes are live knobs too,
                                      # both 0 = off; change the live config only on a measured win
trd engine what-if -p KEY=V1,V2 [-p ...] [--start ISO] [--end ISO] [--json]
                                      # replay EXIT params on the entries the engine really
                                      # took (SB-1343). Each value is a row, each -p its own
                                      # axis, never a grid. Scored PAIRED against a replay of
                                      # the live config on the same trades, in the live trade's
                                      # R — paired SE ~0.015R where the backtest sweep's was
                                      # ~0.05R, and 4s instead of 35min a run. Read-only, so it
                                      # runs against a live home mid-session. Entry knobs
                                      # (entry_delay_minutes, regime_*) are refused: entries are
                                      # fixed here. |t| >= 2 is the bar; check neighbours and
                                      # --start/--end halves before believing one row.
                                      # MEASURED 2026-10-16, day engine, 897 trades: max_sessions
                                      # 0.3-0.75 all t>=2.3, 0.6 best (+0.043R, t=4.5, both
                                      # halves); target_r=1.5 t=2.3 but first half only;
                                      # stop_atr_mult=2.5 worse (t=-3.3)
trd engine backtest --entry-fill open|close    # where a replayed ENTRY fills. 'open' (default on
                                      # intraday bars) reads the settled bars plus the next bar
                                      # just opened — the forming bar a live scan sees — and
                                      # fills at its open. 'close' reads the signal bar complete
                                      # and fills at its close (the only option on daily bars).
                                      # MEASURED 2026-10-16 (SB-1342): 'close' bought +0.35R
                                      # above the live fill on the same trades and scored the
                                      # day engine -0.04R vs live +0.08R; 'open' scores +0.08R.
                                      # Every intraday backtest number from before this was
                                      # read with 'close' entries
trd engine sweep -p KEY=V1,V2 [-p ...] [--start ISO] [--end ISO] [--workers N] [--json]
                                      # backtest sweep in one command (SB-1345): baseline + one
                                      # row per value, one axis at a time, parallel read-only
                                      # replays. Each row: exp R ± SE, Δ vs baseline ± SE (unpaired),
                                      # max DD, capture, exit mix, and exp R per HALF of the window;
                                      # 'significant' = |t| >= 2 AND both halves on the same side.
                                      # For knobs that change WHICH trades exist (entry_delay_minutes,
                                      # regime_*); exit-only knobs measure 3x tighter with what-if.
                                      # 16 rows of 4 months of 5m bars: ~4 min (a single 5m backtest
                                      # went 35 min -> 63 s when volratio stopped re-summing its
                                      # 1,560-bar window every bar — the same speedup reaches scans)
trd engine replay-match [--days 30] [--start ISO] [--end ISO] [--entry-fill open|close] [--json]
                                      # does the backtest trade the way the engine did? Replays
                                      # the window and pairs it with the live trades (same name,
                                      # same session, nearest entry). Splits Σ replay R - Σ live R
                                      # EXACTLY into replay-only trades, live-only trades, and on
                                      # matched trades the ENTRY part (fill/timing) and the EXIT
                                      # part (the live entry's own exit replay vs what it booked).
                                      # Residual must be 0. Read it before believing any backtest.
                                      # Read-only. ~1 min for 30 days of 5m bars.
                                      # Last 30 days, open entries: gap +0.001R/trade, but only
                                      # 129 of 220 trades match — trade SELECTION still differs
trd engine backtest --young N [--strategies K,K]
                                      # the young-listing slice (SB-1434): entries only in the
                                      # first N sessions of names whose first stored bar comes
                                      # after the history's horizon (+7 days) — i.e. names that
                                      # LISTED inside it. Exits run as usual. Prints the slice's
                                      # names and n. Needs a home synced to ONE depth: a name
                                      # 'engine add'-ed with 2y looks like a 2-year-old listing.
                                      # --strategies overrides the rule set for one run
trd engine backtest --settlement/--no-settlement
                                      # T+1: exit proceeds wait a business day before funding an
                                      # entry ('cash_blocked' counts the skips). Default follows
                                      # the engine's capital, as does --capital; replay-match and
                                      # sweep inherit both. MEASURED 2026-10-20, swing config,
                                      # 8y, capital 1000: +0.126R/3,058 trades off vs +0.129R/
                                      # 2,977 on (466 refills deferred), max DD -31.3% -> -28.2%.
                                      # The T+1 rule costs no measurable edge
trd engine backtest --regime/--no-regime        # same history with the regime gate on and off —
                                      # the comparison the gate should be judged on, never assumed
                                      # replay the rules against stored history — same scorecard as
                                      # 'report', hundreds of trades per run. Needs 'trd sync
                                      # --years 10'. Day-mode engines backtest on their intraday
                                      # bars (the walk is keyed on each bar's instant, so
                                      # session_close fires at the bell); day mode on 1d is refused
                                      # READ-ONLY connection (SB-1340): never takes the writer
                                      # lock, never migrates — a home whose schema is behind this
                                      # build is refused, backtest a copy. A universe name with
                                      # no bars lands in 'skipped' instead of aborting the run;
                                      # an unknown name passed via --symbols is still an error
trd engine scan --ndjson --notify     # one JSON event per line (log shipping) + Telegram on fills
trd engine apply-queue [--notify]     # apply the commands the Telegram bot queued for THIS
                                      # engine (TRD_HOME/commands). The bot never opens DuckDB —
                                      # single writer, and it is resident while a scan is not — so
                                      # it queues intents and this is the only thing that drains
                                      # them. Runs before the scan, so a name added from chat is in
                                      # the universe for the very next pass. --notify answers
                                      # whoever typed it, in their own chat
trd engine daily-report [--json] [--date ISO] [--engines swing=/a,day=/b] [--window 30]
                      [--notify]
                                      # post-market report, both engines in ONE message. Leads with
                                      # MONEY: invested (bankroll = size x slots), worth now, and
                                      # the return in dollars and percent, split into cash booked
                                      # and still-open — the 'I put in X, it is worth Y' line. An
                                      # engine on RISK sizing has no bankroll (committed capital
                                      # floats) and the block says so rather than quoting a total
                                      # risked as a total invested. Then today's realized + exits,
                                      # best and worst strategy by expectancy in R over a trailing
                                      # window, today's losses GROUPED BY EXIT RULE, every exit
                                      # today with its legs (entry→exit, hold, rule; best first,
                                      # so line one is the win of the day), and money at risk on
                                      # the open book. Facts only — no judgement.
                                      # 'today' is cash booked by trades that CLOSED today; the
                                      # open book is always *now*, and says so. Stale marks are
                                      # stated at the top, never averaged into a confident number,
                                      # and a date no engine has a bar for sends nothing at all
                                      # (a market holiday must never report as a flat day).
                                      # One database is one engine, so 'both' is two reads summed;
                                      # --engines defaults to TRD_BOT_ENGINES, the same variable
                                      # the bot uses, so chat and the report cannot name engines
                                      # differently. 'trd learn daily-report' defines every line.
                                      # REAL MONEY IS NEVER SUMMED WITH PAPER (SB-1377): a live
                                      # engine gets its own "💵 LIVE (real money)" block FIRST —
                                      # capital, worth now, return $/%, settled vs unsettled cash,
                                      # open intents / awaiting approval, halted — and is excluded
                                      # from MONEY (headed "MONEY (paper)" then) and every both/all
                                      # row; it keeps its own per-engine rows. With the paper twin
                                      # (TRD_LIVE_TWIN, default swing20) in the report: one line,
                                      # trades closed today live vs twin + avg entry gap (live fill
                                      # vs twin fill, %) on names both bought the same session in
                                      # the window, with n. No live engine = byte-identical message
                                      # Every term used carries its definition in the message: the
                                      # report is read by someone learning the vocabulary, so R,
                                      # expectancy and 'at risk' are glossed where they appear
trd engine live-report [--date ISO] [--notify] [--json]
                                      # the LIVE engine's own post-market message (SB-1406):
                                      # account (capital, worth, return — same definitions as
                                      # daily-report's live block — today's move, settled/
                                      # unsettled cash, orders, halt), then every open position
                                      # best first: P&L $/%, R, today's move (vs the previous
                                      # close; the ENTRY for a position opened today), stop in
                                      # force (↑ = trailing) and % below, target and % above,
                                      # at risk; then today's buys, exits and rejected/expired
                                      # orders. Stale marks at the top, and a stale position's
                                      # day move is unknown, never yesterday's. READ-ONLY
                                      # connection, no network. Refused on paper. --notify =
                                      # PRIVATE alert chat only (refused without one), nothing
                                      # sent on a date with no bar. --json {message, sent, report}
trd engine agenda [--engines ...] [--window 10] [--min-sessions 3] [--json]
                                      # which review findings have EARNED a ticket. The review
                                      # recomputes nightly and does not remember yesterday, so a
                                      # month-old problem is reported as a fresh discovery every
                                      # night; this reads the stored reviews back and groups by
                                      # Finding.key (<detector>.<subject>, deterministic — a repeat
                                      # is a string match, not a judgement).
                                      # READY = not a hypothesis AND fired on >= --min-sessions of
                                      # the stored reviews AND still firing. WATCHING = firing,
                                      # unproven, and says WHICH bar is missing. QUIET = fired in
                                      # the window, gone from the newest session — the only evidence
                                      # in trd that a rule change did anything, which is also why a
                                      # once-persistent finding that stopped is NEVER ready.
                                      # The unit of a ticket is the KEY, not the night: 4 stored
                                      # sessions emitted 43 findings from 12 distinct keys, so
                                      # filing per finding per night is ~300 tickets/month for 12
                                      # problems. Numbers come from the NEWEST session — an older n
                                      # is the same analysis over a window that has moved.
                                      # --json carries a ticket title + body per READY finding.
                                      # READ-ONLY, no network, and it knows nothing about Linear:
                                      # trd emits the agenda, an agent files it. 'trd learn agenda'
trd engine watchdog [--engines ...] [--max-age 15] [--repeat 60] [--notify] [--json]
                                      # has a scan landed recently? RUN THIS OUTSIDE THE CLUSTER.
                                      # Reads each engine's published status.json — never a
                                      # database, so it can never block a scan — and alerts when
                                      # the market is open and no scan has landed. Exits non-zero
                                      # so launchd/cron can act without parsing. Silence out of
                                      # hours is correct and never reported; an unchanged alert
                                      # repeats at most hourly; recovery is announced only to
                                      # whoever heard the alarm.
                                      # Live engines also alarm (same file, same dedupe) on
                                      # stuck_intents > 0 — SUBMITTED past its session, or an
                                      # approval past 16:00 unswept — and on a loss-stop halt
                                      # (halted_by max_loss; a hand halt is not an alarm), and on a
                                      # live runner heartbeat (runner.json in the home) missing or
                                      # 25+ min old in the session — staleness only; the runner
                                      # alerts its own failures
                                      # Install: deploy/io.silverbeer.trd.watchdog.plist (every
                                      # 5 min). It must NOT live in k3s: on 2026-09-09 and
                                      # 2026-09-11 the cluster's host channel died, k3s stayed
                                      # up, pods ran against an emptied mount for 19 hours and
                                      # every candidate alarm was inside the thing that stopped
trd bot serve [--passes N]            # Telegram command bot: /add SYM [engines], /rm, /status,
                                      # /book, /report, /engines. Long-polls (no public endpoint,
                                      # cert). User guide: [docs/telegram-bot.md](docs/telegram-bot.md)
                                      # no cert), opens no database, and reads answer from the
                                      # snapshots the scan publishes. Env: TELEGRAM_BOT_TOKEN,
                                      # TRD_BOT_ENGINES='swing=/path,day=/path',
                                      # TRD_BOT_ALLOWED_USER_IDS (numeric ids — usernames are
                                      # changeable and re-registerable, so they are not identity).
                                      # Commands come from the private chat; the channel stays
                                      # one-way. EXACTLY ONE process per bot token (Telegram 409s
                                      # a second). No order placement by design
trd bot check                         # validate the bot's config without taking the poll slot
trd engine reconcile broker.json [--account NAME] [--compare-cash]
                                      # diff a broker snapshot against what trd believes it holds:
                                      # per symbol ok / QUANTITY / MISSING AT BROKER / UNTRACKED,
                                      # plus how far trd's stored close sits from the broker's mark
                                      # (with its date — a gap there is stale bars, not bookkeeping).
                                      # Exits non-zero when they disagree. No network, no credentials:
                                      # the brokerage read is an authenticated MCP session that writes
                                      # the snapshot ([docs/broker-mcp.md](docs/broker-mcp.md))
                                      # --compare-cash: snapshot "cash" (settled) and optional
                                      # "unsettled" vs the engine's T+1 ledger; beyond CASH_TOLERANCE
                                      # ($0.05) = CASH MISMATCH, exit 1. OPT-IN because the broker
                                      # account usually holds more than the engine's capital
                                      # — only for dedicated cash
```

## Deployment

Unattended runs live in two places — use one, never both (DuckDB is single-writer):

- **k3s CronJob** (the real one): [k3s/trd-engine/README.md](k3s/trd-engine/README.md).
  `./scripts/deploy-k3s.sh --test` seeds `~/.trd-engine`, builds + imports the image,
  applies the manifests with the hostPath rewritten to this machine, and runs one scan.
  Telegram setup (bot, chat id, verification) is documented there.
- **launchd agents** (no cluster): [deploy/README.md](deploy/README.md) — Sunday Prep,
  the engine scan, and `engine-publish.sh`, which copies the engine's `status.txt` and
  `engine-backup.json` into iCloud. The publisher pairs with *either* runner because it
  only copies files and never opens the database.

The post-market report is its own CronJob (`k3s/trd-engine/daily-report-cronjob.yaml`,
`./scripts/deploy-k3s.sh --report`): 16:16 ET on weekdays, mounting *every* engine home
and sending one message. Deployed once, not per engine — one message covering all is
the whole point, and one per engine would be the fill alerts again. Its combined row
reads `both` for exactly two engines and `all` for more. It runs after the scan
CronJob's entrypoint has stopped for the day (it refuses past 16:00), and off the
ten-minute grid the queue drain uses, so it is not waiting on the writer lock.

Every home's queue drain (`deploy/queue-entrypoint.sh`) also runs the **close sync**
(SB-1411): once per weekday on the first tick past 16:05, `trd sync --require-settled`,
stamped in `TRD_HOME/.close-sync` only on success, retried each tick up to 6 tries. The scan syncs once in the morning and
stores no bar after it, so without this every evening report read the 09:3x bar as the
close (PANW 355.01 reported, 380.55 actual, 2026-10-23).

The nightly decision review is the third CronJob (`k3s/trd-engine/review-cronjob.yaml`,
`./scripts/deploy-k3s.sh --review`): 16:31 ET weekdays, every home, `trd engine review
--ai --snapshot --notify`. Same shape as the report and deployed once for the same reason.
The image carries the `ai` extra for it; the scan never imports it.

The live engine has its own post-market message, `k3s/trd-engine/live-report-cronjob.yaml`
(`trd-live-report`): 16:20 ET weekdays, mounting the live home ONLY, `trd engine
live-report --notify` to the private alert chat. Per-home, so `./scripts/deploy-k3s.sh
--live` applies it with the scan and queue drain, never `--report`.

The Telegram command bot is a **Deployment**, not a CronJob — long polling has to stay
resident: `k3s/trd-engine/bot-deployment.yaml`. `replicas: 1` and `strategy: Recreate`
are correctness, not capacity: Telegram allows one `getUpdates` per bot token and answers
a second with a permanent 409, which a RollingUpdate would cause on every deploy. It
shares the engines' volumes but never opens a database — see the command-bot section of
[k3s/trd-engine/README.md](k3s/trd-engine/README.md).

The engine's DB (`~/.trd-engine`) is deliberately separate from the real one and never in
iCloud: a k3s pod can't see a macOS FileProvider path, and iCloud resolves binary
conflicts by duplicating rather than merging.

One database is one engine: `EngineConfigRepo.get()` takes the most recently created
config, so the engines are separate `TRD_HOME` directories with separate CronJobs:
swing (`~/.trd-engine`, `trd-engine-*`), day (`~/.trd-day`, `--day`) and swing20
(`~/.trd-swing20`, `--swing20`) — the paper copy of the future live engine, 5 x $20 on
$100 capital with the cash ledger on, seeded BY HAND with swing's universe and
strategies (the script never reads another engine's DB). The live engine's operator
guide — what it is, turn it on, turn it off, what each alert means — is
[docs/live-engine.md](docs/live-engine.md). Anything addressing "every
engine" addresses three homes, and the report, review and bot mount all three — four
once `~/.trd-live` exists: `--live` deploys the live engine (`trd-live-*`, label
`💵 live`), never seeds it, refuses a home that isn't `mode: live`, and refuses
`--test`. The shared manifests carry the live mount on lines marked `# live-only`,
deleted by the script while the home is missing, because a `type: Directory` mount on a
missing path takes the report and the bot down with it.

Telegram names an engine by its label — `swing`, `day`, `swing20` — set as
`TRD_ENGINE_LABEL` by the deploy script (`ENGINE_LABEL`), never by the Kubernetes name;
it is the same word the bot, report and review use. swing20 pushes no fills
(`TRD_SCAN_NOTIFY=0` drops the scan's `--notify`); its chat replies and its place in
the report and review are unchanged.

## Output contract

Read commands take `--json`: the underlying model, full precision, stable keys, no colour,
no truncation, one document on stdout. Errors under `--json` are `{"error", "message"}` on
stdout with a non-zero exit, so one `| jq` handles success and failure alike. Spinners are
suppressed in JSON mode (they write to stdout and would corrupt the document). Rich tables
truncate to terminal width — never parse them.

CSV import format (header required): `date,account,symbol,side,quantity,price[,fees,note]` — date is ISO, side is buy/sell.

## Architecture rules (enforce in review)

- CLI layer ([src/trd/cli](src/trd/cli)) never touches DuckDB or yfinance directly — services only.
- Services ([src/trd/services](src/trd/services)) never import Typer/Rich — pure logic, fully testable.
- All market data goes through the `MarketDataProvider` protocol ([src/trd/providers/base.py](src/trd/providers/base.py)). Never import yfinance outside [src/trd/providers/yf.py](src/trd/providers/yf.py).
- Holdings are always derived from transactions via FIFO ([src/trd/services/fifo.py](src/trd/services/fifo.py)) — never stored as mutable balances.
- `earnings_event` is a rewritable cache of *dates* (what the entry blackout needs). `earnings_result` ([src/trd/services/earnings_archive.py](src/trd/services/earnings_archive.py)) is the **point-in-time archive**: written when a release is first announced so the estimate is captured before the number lands, never revised afterwards, and `quality_status` names what was genuinely observed so an unfillable column (revenue, guidance, revisions, BMO/AMC timing — none of which yfinance supplies) never reads as a measurement of zero. It is the one provider-sourced table in `trd backup`, because no provider can hand it back.
- An instrument carries `tradable`. False means a calculated number rather than a holding — `^VIX`, which the regime gate reads and nothing can own. It stays typed `stock` because `instrument.type` has a `CHECK` from `001_init` that DuckDB cannot widen and 12 foreign keys point at the table. `record_trade` and `trd engine add` refuse a non-tradable; `EngineService.universe()` filters one already in a list; the earnings poll skips it (an index never has a date, so it qualified as "needs a re-check" on every scan — ~158 wasted requests a day).
- Schema changes = new numbered file in [src/trd/db/migrations](src/trd/db/migrations). Never edit an applied migration.
- **Cash received is not a trade.** Income (dividends, interest, a broker's sweep) lives
  in its own table ([migration 024](src/trd/db/migrations/024_income.sql)), never as a
  third value on `Side` — that enum is load-bearing in FIFO, and a dividend creates no lot
  and consumes none, so the holdings arithmetic must not be able to see it. It does reach
  every return that measures money, and does so through ONE definition:
  [src/trd/services/cashflow.py](src/trd/services/cashflow.py). Three places built XIRR
  flows by hand before it (dashboard, equity curve, plan detail); three copies is how
  income gets added to two of them and quietly missed by the third, and a return that is
  right on one screen and wrong on the next is worse than one that is wrong everywhere.
  A plan's share of a payment is pro-rated by the shares the plan held that day. `trd
  backup` carries it (version 5) because no provider can hand it back — a provider knows
  what a symbol has paid, never which payments landed in this account.
- **A fee is money out that LOWERS the return — never a withdrawal.** An advisory fee
  (cash_flow kind `fee`, SB-1418) leaves the account's cash, but the owner did not take
  it, so it is never an external flow: `cashflow.external_flows` (the cash-based returns
  — alloc view/report XIRR, simple return, same-deposits SPY) leaves it out so the lower
  worth shows, and `cashflow.fee_flows` puts it into the trade-based ones (dashboard,
  equity curve) as money the owner paid. `Dashboard.total_return_pct` subtracts it.
  Booking a fee as a withdrawal would excuse it, and a managed account would score as
  well as one charging nothing.
- **Price return and total return are different numbers and trd says which.** `Dashboard.
  price_return_pct` is (value − cost)/cost; `total_return_pct` adds the income. Reporting
  the first under the second's name is what made every figure quietly low.
- Money/quantities are `Decimal` end to end. Never float.
- Broker integration is **agent-side only**: an MCP session reads the brokerage and writes a snapshot file; `trd engine reconcile` does the diff. Nothing under `src/trd` imports or knows about MCP. Unattended, that session is the **live runner** (`deploy/live-runner.sh`, launchd, SB-1382): headless `claude -p` makes the broker calls, and `deploy/broker_stream.py` reads what came back from the stream-json transcript — the broker's JSON as returned, never the model's retelling — and halts the engine on any order call no outbox spec explains. It runs with `--setting-sources local` plus `deploy/live-runner-settings.json`, pinned in `~/.trd-runner` by `live-runner.sh install`; the phone approval in the broker's app is the per-order human gate, and it refuses to place when approvals are off. The committed `.claude/settings.json` (never `settings.local.json`, which is gitignored and would put the gate on one machine only) names every tool the server has exposed: 53 reads allowed, 3 asked, 27 denied. The asked three are the equity order path (`review_equity_order`, `place_equity_order`, `cancel_equity_order`, SB-1373): `ask` prompts on every call, on top of the broker app's own trade approval. The denied are the 24 other writes (option/advanced/crypto order place and cancel, option exercise, declining a trade approval, watchlist, scan and alert mutations) plus the three non-equity order previews (`review_option_order`, `review_advanced_order`, `preview_crypto_order`), denied because trd decides from its own data. The list drifts: The broker added 30 tools, 11 of them writes, that sat unlisted until SB-1364. There is no mid-name wildcard, so a tool added later matches neither list and surfaces as an unlisted tool needing an explicit decision. See [docs/broker-mcp.md](docs/broker-mcp.md).
- The Telegram command bot ([src/trd/notify/bot.py](src/trd/notify/bot.py)) **never opens the database**. DuckDB is single-writer and the bot is resident while a scan is not, so a connection held there would lock out a scan — putting a chat feature in the trading path. Reads answer from the snapshots the scan publishes (`status.json`, `report.json`, `status.txt`); writes go to a queue of JSON files under `TRD_HOME/commands/` that `trd engine apply-queue` drains inside the scan's process. Queue files are named by Telegram's `update_id` so replay order is the order typed and a redelivered update is recognised, not reapplied. Authorization is a numeric-user-id allowlist checked before anything reads the message text.
- Static reference data (curated universe, FOMC/macro calendar) lives in [src/trd/data](src/trd/data) as plain Python — no YAML dep. `SundayPrepService` is pure (provider + data, no DuckDB); its briefing narrative is deterministic templates, leaving a seam for a future `--ai` pass.
- **The only code in trd that calls a model lives in [src/trd/agents](src/trd/agents)**, is
  reached through an optional `trd[ai]` extra, and is imported lazily by the CLI — so the
  engine, the scan image and every test stay runnable on a machine with no API key, no
  network and no LLM SDK. Nothing under `services/` imports it, the same rule that keeps
  Typer out. Its tools are all reads and return pydantic models (trd's read surface is
  already typed, so a tool wrapping the pack needs no adaptation); its output is a
  validated model, which is what makes "works with several providers" a real claim rather
  than a hope — a weaker model that drifts fails validation instead of emitting plausible
  prose. There is no tool that moves a stop, edits config or places an order: read-only is
  a property of the tool surface, not of the prompt. Tests drive it through pydantic-ai's
  test models with `ALLOW_MODEL_REQUESTS = False`; the suite never reaches a provider.
  Tools return **projections** (`TradeBrief`, `SignalBrief`, `WindowSummary`), never the
  storage models: the fields a judgement uses, at two places. Which backend runs is decided
  in `resolve_model` alone — pydantic-ai strings pass through, `claude-code:` shells out to
  the local binary ([src/trd/agents/claude_code.py](src/trd/agents/claude_code.py), which
  strips ANTHROPIC_API_KEY from the child so the subscription is billed, not the key) and
  `vertex:` wraps the Anthropic model in a Vertex client. Nothing else learns which.
- **trd emits work; it never files it.** `trd engine agenda`
  ([src/trd/services/agenda.py](src/trd/services/agenda.py)) reads the stored reviews
  and says which findings have earned a ticket — it does not create one. Nothing under
  `src/trd` imports, or knows the name of, an issue tracker: no client, no API token in
  the cluster, and a nightly CronJob that cannot fill a backlog unattended. Same rule as
  the broker's MCP session, and for the same reason — the thing that trades money does
  not reach out and write to other systems. The agenda is a read: `connect_read_only`,
  so it can never take the writer lock a scan needs.
- **A trade is described; a rule is judged.** The per-trade grades
  ([src/trd/services/verdicts.py](src/trd/services/verdicts.py)) turn the stored
  MAE/MFE/capture/follow-through into sentences — "offered +2.5R and never traded below
  what we paid", "price carried on another +0.85R without us". They are observations
  about a price path, true whatever anyone thinks of the rule. A *conclusion* ("this
  filter discards winners") needs a population and stays in a `Finding` with its `n`.
  Grading one trade as a good or bad decision is the thing the review exists not to do:
  a correct decision loses money often enough that trade-by-trade verdicts teach the
  opposite of what the measurement is for. The buy grade reads MFE and nothing else, so
  an exit can never colour it — that independence is what makes "good buy, bad exit"
  sayable at all.
- The decision review ([src/trd/services/review.py](src/trd/services/review.py)) is the
  deterministic half of the daily agent, and the split is deliberate: the pack is the
  agent's *input contract* (so a model's answer can be re-run and diffed), and the
  findings are statistics (so most of what a review does is testable). A model, when it
  arrives, consumes the pack and adds prose — it never replaces a detector. Every
  threshold is a module constant, because a detector with a tuned threshold is a model
  with extra steps.
- Outcome measurement ([src/trd/services/outcomes.py](src/trd/services/outcomes.py)) is
  arithmetic and stays that way: no LLM, no judgement, no "this trade was a mistake".
  It computes MAE/MFE/capture/follow-through per closed trade and the same walk over
  every *unacted* signal, so anything reasoning about the engine later argues from
  measurement rather than from memory. The counterfactual stop comes from
  `plan_entry`'s own `stop_distance`, shared with the live fill path — two copies of
  that line would make a hypothetical R and a real R quietly different units.
- An exit rule may sell PART of a position: `ExitDecision.fraction` (1 = all, the default
  every rule but `scale_out` returns) and the shared `exit_quantity` both the live scanner
  and the backtest size from — a second copy of that rounding is how the two would quietly
  stop scoring a scaled-out trade the same. A partial books cash and part of the R through
  `book_exit`, leaves the stop/target/trail untouched, writes no exit reason and keeps its
  slot. `scale_out` is off by default and the measurement says leave it off.
- Engine rules are code-registry entries, never config: entry strategies in [src/trd/engine/strategies.py](src/trd/engine/strategies.py) (`@register`, mirroring the indicator registry), exit rules in [src/trd/engine/exits.py](src/trd/engine/exits.py). Strategies never reimplement indicator math — they call the indicator registry. Every signal and exit carries a plain-English `reason`; a rule you can't explain doesn't ship.
- **Every rule lookback is denominated in sessions, never bars.** "20-day" means twenty sessions on every timeframe; `sessions_to_bars` resolves it. Exits do this inline; entries get a `StrategyContext` carrying two series — the engine's own bars for the *trigger*, settled daily bars for the *trend filter*, which has to read daily because 200 sessions of 5-minute bars is 15,600 of them and the provider serves ~4,600. `ctx.daily` never includes the session in progress, on any timeframe: that is the lookahead guarantee for the trend, as the prefix slice is for the bars. A new rule that hardcodes a bar count reintroduces the bug both fixes exist to kill.
- **A day engine closes a position from an earlier session on sight.** `session_close`
  fires on `opened_at.date() < now.date()` as well as at `flat_at_minute`: 09:31 is before
  15:55, so without it a trade the engine was holding when it stopped simply ran on. It
  cost -7.4R over three gap fills on 2026-09-10 (SB-1054).
- **`hostPath` mounts are `type: Directory`, never `DirectoryOrCreate`.** The engine homes
  are shares from the Mac; when that share died mid-session, DirectoryOrCreate had
  Kubernetes create an empty directory in its place and every pod then ran happily against
  no database. `Directory` fails the mount instead. The scan entrypoint refuses to start on
  a missing or empty `trd.duckdb` for the same reason, naming the share as the likely cause.
- An engine is **paper** or **live** (`engine_config.mode`, migration 027 — a mode on the config, never a third account type, because `account.type`'s `CHECK` cannot be widened). A **paper** engine trades a `simulation` account and fills its own decisions as ordinary `txn` rows — so portfolio/equity/XIRR/drawdown work on it unchanged. A **live** engine trades a `real` account and fills nothing: where paper would write a txn, the scan writes an `order_intent` (side, quantity, the price it saw, the entry plan or the exit rule, a uuid `ref_id` the broker uses as its idempotency key) and leaves `engine_position` alone. The fill is the broker's and is ingested separately, at the real price, with the stop re-derived from the intent's `atr_at_entry` by `stop_distance` so live R is measured from what was paid. An open intent claims what it asked for: a buy holds a slot, the name, its entry-budget place and its notional in the cash ledger; a sell blocks a second exit on the same position. Nothing under `src/trd` places an order — the intent is the hand-off to the agent-side broker session. Either way, `engine_position` stores only what a txn can't: strategy, stop/target, trail high-water mark, exit reason. The initial stop is immutable so closed-trade R-multiples stay meaningful.
- Tests never hit the network. Extend `FakeProvider` in [tests/conftest.py](tests/conftest.py).

### Privacy (public repo)

This repo is public and trd tracks real accounts: **no real account data ever enters the
repo or its git metadata**, and **Telegram never says where money is held**. Rules, hooks,
CI and the send guard: [docs/privacy.md](docs/privacy.md).

- Real data lives in `TRD_HOME` only (`~/.trd-portfolio`, the engine homes). Tests use
  synthetic data — invented figures, public tickers, `TEST-ACCT-*` ids; an export-shaped
  fixture is listed in [tests/fixtures/SYNTHETIC.md](tests/fixtures/SYNTHETIC.md).
- Commit messages, PR titles/bodies and branch names never carry real figures, account
  numbers or brokerage names; code and docs carry none either (SB-1427).
- Every new clone runs `scripts/install-hooks.sh`. `--no-verify` is visible in review; CI
  scans the tree and the PR text regardless. Exceptions go in
  `scripts/privacy_exceptions.txt` with a reason; private terms in
  `~/.config/trd/private-terms.txt`, never in the repo.
- Every Telegram message passes `trd.notify.privacy.guard` (in `TelegramNotifier.send` and
  the bot transport) and is refused whole on a match. Renderers never print a stored fund,
  account name or firm: "a money market fund", "your broker app".

## Environment

- DB lives at `~/.trd/trd.duckdb`; override root dir with `TRD_HOME` (tests do this).
- `trd` table for transactions is named `txn` (`transaction` is a reserved word).
