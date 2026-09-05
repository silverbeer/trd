# trd engine on k3s

One `trd engine scan` every 5 minutes during the regular session, as a CronJob.
Replaces the launchd agent in `deploy/` — **do not run both against the same
database**, DuckDB allows one writer.

## Deploy

Two commands. That's the whole thing.

```bash
cd ~/gitrepos/trd && git pull && uv tool install --editable .
./scripts/deploy-k3s.sh --test
```

The script prints the target context and waits for a `y` — the engine writes
trades, so deploying to the wrong cluster is not a no-op. Then it:

1. **Seeds `~/.trd-engine` if it's empty** — creates the paper account, the
   10-symbol universe, and downloads 2 years of daily bars (the rules need 200).
   Skipped if already seeded.
2. Builds the image and imports it into k3s.
3. Applies the namespace and CronJob, **rewriting the hostPath to this machine's
   home** — no hand-editing the manifest per user.
4. With `--test`, runs one scan immediately, market-hours guard bypassed.

### Running more than one engine

`--day` deploys a second engine against `~/.trd-day`:

```bash
./scripts/deploy-k3s.sh --skip-build --day
```

The two share the image, the namespace and the Telegram secret, and differ only
in which database they mount — a swing engine carrying positions overnight, and
a day-mode one that flattens at `flat_at_minute`.

The CronJob **name** is what keeps them apart (`trd-engine-scan` vs
`trd-day-scan`). Without that, applying the manifest twice would replace the
first engine and silently repoint it at the other database, which looks like
nothing happening until you notice one engine's trades landing in the other's
account.

Each engine's pods carry `component: <engine-name>`, so:

```bash
kubectl logs -n trd -l component=trd-day --tail=100 -f   # one engine
kubectl logs -n trd -l app=trd --tail=100 -f             # all of them
```

`concurrencyPolicy: Forbid` is per CronJob, so it does not stop two *different*
engines running at once — that is fine, because they hold separate DuckDB files
and never contend for the same writer lock.

### `~/.trd-engine` is not your real database

It is a separate, paper-only DuckDB holding one simulation account and its
universe's price history. Your real trd database is never opened by any of this —
the engine cannot reach it and does not need it, because it only trades paper.

Override the location with `ENGINE_HOME=/some/path ./scripts/deploy-k3s.sh`.

## Telegram feed

Fills — and only fills — get pushed. Scans are quiet the overwhelming majority
of the time; pushing every pass would train you to ignore the channel.

### 1. Create the bot

Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and a
username. It replies with a token like `123456789:AAH...`. That token is the
password to the bot — treat it like one.

### 2. Pick where messages land, and get its chat id

**The two options behave differently, and this is where people get stuck.**

<details open>
<summary><b>Option A — a channel</b> (recommended: readable on phone and Mac, easy to mute)</summary>

1. Create a channel in Telegram.
2. **Add the bot as an administrator** with "Post Messages" permission. A bot
   that is merely a member cannot post, and the API returns 403.
3. Post any message in the channel yourself.
4. Read the id — note `channel_post`, **not** `message`:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | jq '.result[].channel_post.chat.id'
```

Channel ids are negative and begin with `-100`, e.g. `-1001234567890`.
</details>

<details>
<summary><b>Option B — a direct message to yourself</b> (simplest)</summary>

1. Open a chat with your bot and send it `/start`.
2. Read the id:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | jq '.result[].message.chat.id'
```

Direct chat ids are positive.
</details>

If `getUpdates` returns `{"ok":true,"result":[]}`:

- you haven't posted since creating the bot — post again, then retry;
- or a webhook is set, which suppresses `getUpdates` entirely —
  clear it with `curl -s "https://api.telegram.org/bot<TOKEN>/deleteWebhook"`;
- or you used the wrong JSON path for your case (see the two options above).

### 3. Prove it works *before* deploying

Do not skip this. It takes five seconds and turns a silent misconfiguration into
an immediate answer:

```bash
curl -s -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id='<CHAT_ID>' -d text='trd engine test' | jq '.ok'
```

`true` and a message on your phone means both values are right. `false` comes
with a `description` naming the problem — usually `chat not found` (wrong id) or
`bot is not a member of the channel chat` (step 2.2 skipped).

### 4. Create the secret

```bash
kubectl create secret generic trd-engine-telegram \
  --namespace trd \
  --from-literal=TELEGRAM_BOT_TOKEN='123456789:AAH...' \
  --from-literal=TELEGRAM_CHAT_ID='-1001234567890'
```

The namespace only exists after a deploy, so run this after
`./scripts/deploy-k3s.sh`. Then trigger one scan to confirm the wiring:

```bash
JOB=tg-test-$(date +%s)
kubectl create job --from=cronjob/trd-engine-scan "$JOB" -n trd
kubectl set env job/"$JOB" -n trd TRD_ENGINE_FORCE=1
kubectl logs -n trd job/"$JOB" -f
```

You'll only get a Telegram message if that scan actually filled something. To
prove the path end to end regardless, run the `curl` from step 3.

### Which engine sent it

One secret feeds every engine, so a swing engine and a day engine push to the
same chat — and the same symbol can sit in both universes. Fills are therefore
prefixed with the sender:

```
[trd-day] 🟢 BUY MSFT x2 @ 512.40
```

The deploy script sets `TRD_ENGINE_LABEL` to the engine's name, so this needs no
configuration. Running the CLI by hand (`trd engine scan --notify`) with no such
variable set falls back to `swing` or `day`, read from whether the rule set has a
`flat_at_minute`. Set `TRD_ENGINE_LABEL` yourself to name an engine anything else.

Want the two feeds separated entirely? Create a second channel and give the day
engine its own secret — the CronJob's `envFrom` name is the only thing to change.

## The queue drain — chat outside market hours

The queue is normally drained inside the scan, which is the process that safely
holds DuckDB's single writer. That is right during the session and useless
outside it: `engine-entrypoint.sh` exits at its market-hours guard long before it
reaches the drain, and the scan CronJob does not fire in the evening at all. A
`/add` typed at 17:23 waited sixteen hours.

`${ENGINE_NAME}-queue` is a second CronJob per engine home that closes that gap:

```bash
./scripts/deploy-k3s.sh            # deploys the scan AND the queue drain
kubectl get cronjob -n trd         # trd-engine-scan, trd-engine-queue, ...
```

It runs `*/10 * * * *`, every day, and **skips 09:25–16:05 on weekdays** because
the scan already drains the queue there — before it scans, so a name added from
chat is in the universe for the very next pass.

Two CronJobs are not covered by each other's `concurrencyPolicy`; it is scoped to
one. A drain that grabbed the writer as a scan started would fail **the scan**,
which is worse than a late `/add`. Hence the window guard, deliberately wider than
09:30–16:00 at both ends, and a `DatabaseBusyError` treated as success rather than
a failed Job — belt and braces, because the cost of meeting a scan is asymmetric.

Force one for testing, ignoring the window:

```bash
kubectl run trd-queue-now -n trd --rm -i --restart=Never --image=trd:latest \
  --image-pull-policy=Never --env TRD_QUEUE_FORCE=1 --command -- /app/queue-entrypoint.sh
```

## Outcome measurement — after the close, not during

The last scan of the day (15:55 or later) runs `trd engine outcomes --backfill`
before it publishes its snapshots. Nothing else has to be scheduled.

It is deliberately not run mid-session. The measurement walks the bars a trade
lived through, and the follow-through window — where price went *after* the exit
— does not exist yet at 10:05 for a trade that closed at 10:00. Measuring then
and skipping it later on the grounds that a row exists would make the engine
permanently believe nothing ever happened after any exit it took.

So a row is skipped only once it is **final**: once the walk has seen its whole
horizon. A swing trade closed today is re-measured on each of the next five
sessions as its future fills in, and then never again. That is why the pass is
cheap to repeat and why `--backfill` is safe to run by hand at any time.

Failure is logged, never fatal. Measurement is not the trading path, and a scan
that worked must not be reported as failed because a statistic could not be
computed.

## Post-market report — was the day any good

The fill feed says a trade happened. It never says whether the day was good, which
is how a system that runs every day stops being read. One CronJob fixes that with
one message after the close, covering both engines:

```bash
./scripts/deploy-k3s.sh --report      # 16:16 ET, weekdays, swing + day in one message
```

Run it by hand against the live homes, no schedule and no Telegram:

```bash
TRD_HOME=~/.trd-engine trd engine daily-report \
  --engines swing=$HOME/.trd-engine,day=$HOME/.trd-day
```

Send one now from the cluster:

```bash
kubectl create job -n trd --from=cronjob/trd-engine-report report-now
kubectl logs -n trd -l component=report --tail=50
```

### What it says, and what each word means exactly

```
📊 trd daily — Thu Sep 3

TODAY
swing  +5.17 · 5 exits
day    +0.24 · 10 exits
both   +5.40 · 15 exits

SINCE START
swing  realized +89.80 · unrealized +18.68 · NET +108.49
...

WORKING (30d)
swing  Pullback +0.13R · 15 trades · 53% win

NOT WORKING (30d)
swing  Momentum -0.36R · 8 trades · 12% win
today's losses by exit
  Stop Loss x3 -18.40

TODAY'S TRADES
swing
  COIN +6.46 (+0.59R) · 172.13 → 183.26 · 10 sessions · Time Exit
  CRWD -6.52 (-0.59R) · 218.40 → 204.15 · 4 sessions · Indicator
day
  HOOD +0.41 (+2.24R) · 116.83 → 121.57 · 48m · Profit Target
  +5 more
  SNOW -0.42 (-1.10R) · 374.18 → 358.43 · 5h 50m · Stop Loss

OPEN BOOK (now)
swing  10 open · unrealized +18.68 · at risk 96.27
day    flat
```

Three different periods appear in one message, so the words are load-bearing:

- **TODAY** is cash booked by trades that *closed* today. A trade still running is
  not in it, however well it is doing.
- **SINCE START** is every trade the engine has taken. NET is realized plus
  unrealized and never appears without both halves — an engine up only on open
  positions is a different engine from one up on closed ones.
- **WORKING / NOT WORKING** rank strategies by expectancy in **R** over a trailing
  window, not in dollars: R is what makes a $200 day trade and a $2,000 swing
  comparable. A strategy with fewer than three closed trades in the window is not
  named — that is an expectancy, not evidence.
- **today's losses** are grouped by the exit *rule*. Three stops is a broken
  thesis; three session closes is a day engine that never got paid; one total
  cannot tell them apart.
- **TODAY'S TRADES** names every exit with its legs: dollars and R, entry → exit,
  how long it was held, and the rule that ended it. Best first, so the top line is
  the win of the day. Hold reads in sessions on a swing engine and in elapsed time
  on an intraday one. A long day is trimmed from the *middle* — a best-first list
  cut to a prefix would hide every loser behind "+5 more".
- **OPEN BOOK** is *now*, not the close: reconstructing a point-in-time book would
  need marks the engine does not store.

`trd learn daily-report` is the same definition list, on the machine.

### When it stays quiet, and when it warns

- **A date no engine has a bar for sends nothing.** No trading calendar is needed:
  a market holiday is a day nobody stored a session for. A report that posted
  "flat, nothing happened" every Thanksgiving would train its reader to ignore it.
- **Stale marks are stated at the top**, above the numbers they would break. A
  symbol that lost the 09:30 race to publication keeps yesterday's close, and
  every figure drawn from it — unrealized, risk, NET — is quietly wrong.
- **An unreadable engine home is named, not fatal.** Losing the swing engine's
  numbers because the day engine is unmounted is exactly how a daily report stops
  being trusted. The job still exits 0: a named problem in the message beats a pod
  that simply failed.

### Why 16:16, and why one job for both engines

The day engine flattens at 15:55 and the scan entrypoint refuses to run past
16:00, so nothing holds the writer lock by then. The `:16` is deliberate too —
the queue drain fires on the ten-minute grid, and DuckDB has one writer.

One database is one engine, so "combined" is two reads summed rather than one
query, and the job mounts both homes exactly as the bot does. Deploying it per
engine would produce two messages a night, which is the fill feed again.

## Command bot — driving the engines from chat

The feed above is one-way. `trd bot serve` makes the same bot two-way, so
"put PLTR in front of both rule sets" is a message rather than an ssh session:

```
/add PLTR             queue a universe add on every engine
/add PLTR day         …or just one
/rm PLTR swing        stop new entries in a name (an open position is left alone)
/status               build, capacity, bar depth, realized/unrealized/net, risk
/book                 the open book and the scorecard, as last published
/report               per-strategy expectancy
/engines              which engines this bot drives
```

There is deliberately no order placement. The engines trade simulation accounts
and entries and exits stay with the rules; chat changes what is *eligible* to be
traded, never what is traded.

### Where commands are typed, and by whom

**Commands go in your private chat with the bot, not the fills channel.** A
channel post arrives as `channel_post` with no reliable sender, so there would be
nobody to authorize — and the channel should stay a loudspeaker. One bot, one
token, two chats: the channel gets fills, the DM takes commands and gets the
answers.

Authorization is your numeric Telegram user id, in
`TRD_BOT_ALLOWED_USER_IDS`. Not your username: usernames are changeable by their
owner and re-registerable by somebody else once released, so they are not an
identity. Get yours by sending the bot `/start` and reading:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | jq '.result[].message.from.id'
```

Anyone not on the list is ignored with no reply — a refusal would tell a scanner
the bot is live and worth working on.

Add it to the existing secret (recreate it; secrets are not patchable in place):

```bash
kubectl delete secret trd-engine-telegram -n trd
kubectl create secret generic trd-engine-telegram -n trd \
  --from-literal=TELEGRAM_BOT_TOKEN='123456789:AAH...' \
  --from-literal=TELEGRAM_CHAT_ID='-1001234567890' \
  --from-literal=TRD_BOT_ALLOWED_USER_IDS='123456789'
```

### How it avoids the database

DuckDB has one writer, and the bot is resident while a scan is not — so a bot
holding the file would lock out the 09:35 scan, putting Telegram in the trading
path. It never opens the database at all:

- **reads** answer from `status.json`, `report.json` and `status.txt`, which the
  scan entrypoint publishes every pass. As fresh as the last scan;
- **writes** append an intent to `TRD_HOME/commands/`, and `trd engine
  apply-queue` — the first thing the entrypoint runs — applies them. The engine
  stays the only writer, and a name added from chat is in the universe for the
  very next pass.

So a write lands within one scan interval (≤5 min during the session), and the
ack says so. The confirmation comes back after it is really applied, and reports
bar depth: a name with fewer bars than its rules need is in the universe and
invisible to every strategy, which reads as a broken engine rather than one
warming up.

### Deploying it

One database is one engine (`EngineConfigRepo.get` takes the most recent
config), so day and swing are separate homes and the bot is told about both:

```yaml
- name: TRD_BOT_ENGINES
  value: swing=/engines/swing,day=/engines/day
```

```bash
./scripts/deploy-k3s.sh --bot --skip-build
kubectl logs -n trd -l component=bot -f
```

The script rewrites both `hostPath`s for this machine — the committed
`/Users/tomdrake/...` is a default, exactly as it is for the CronJobs — and
refuses to apply anything if the secret has no `TRD_BOT_ALLOWED_USER_IDS`, or if
an engine home does not exist. Both refusals print the fix: a pod that
crash-loops with the answer buried in its logs is a worse way to learn either
one. It then asserts that exactly one poller is running.

### Finding your numeric user id

The allowlist takes a numeric id and Telegram does not show you your own.
`TRD_BOT_ALLOWED_USER_IDS` rejects usernames on purpose — they are changeable
and re-registerable, so they are not identity.

```bash
# 1. send any message to the bot in Telegram
# 2.
./scripts/telegram.sh whoami
user_id=123456789  username=you  chat_id=123456789  chat_type=private
```

It refuses to run while the bot is up: `getUpdates` allows one caller per token,
and the second gets the 409 — asking would knock the running poller off its own
poll, trading a diagnostic for an outage.

`chat_type` is worth reading. Commands are only taken in a **private** chat, so a
`group` there means the message you sent will never be answered no matter what
the allowlist says.

### Talking to the API without a pod

`scripts/telegram.sh` covers the rest of it — `check` asks whether the token is
live and which bot it belongs to, `send` posts a message, `whoami` is above:

```bash
./scripts/telegram.sh check
token source: 1Password (op://Personal/Telegram Bot Tokens/trd-engine-bot)
ok — @trd_engine_bot (id 8959767886)
```

The token is resolved from `$TELEGRAM_BOT_TOKEN`, then 1Password, then the cluster
secret, and is never printed. 1Password comes before the cluster deliberately: a
laptop that can reach the vault does not need kubectl, so this still answers when
the cluster is down — which is one of the times you most want to ask whether the
token works.

### Rotating the token

Treat the token as compromised if it has ever been pasted anywhere that keeps a
record — a chat window, a terminal transcript, a CI log. Anyone holding it can
read every message sent to the bot and post as it, and **the allowlist does not
help**: it filters which *users* may command the bot, not who may hold its
credentials.

```bash
# 1. BotFather -> /revoke -> the bot -> new token
# 2. In a terminal, with `read -rs` so it is never echoed or saved:
read -rs TOKEN && kubectl patch secret trd-engine-telegram -n trd \
  -p "{\"stringData\":{\"TELEGRAM_BOT_TOKEN\":\"$TOKEN\"}}" && unset TOKEN

# 3. Restart the poller so it picks the new token up:
kubectl rollout restart deploy/trd-engine-bot -n trd
```

The CronJobs need no restart — each scan is a fresh pod that reads the secret at
start. Revoking immediately invalidates the old token, so the only cost of
rotating is the restart above.

Verify the configuration without taking the token's poll slot:

```bash
kubectl exec -n trd deploy/trd-engine-bot -- trd bot check
```

**`replicas: 1` and `strategy: Recreate` are correctness, not capacity.**
Telegram permits one `getUpdates` per bot token and answers a second with a
permanent `409 Conflict` — two replicas fight over the token, and a
`RollingUpdate` recreates that collision on every deploy. The bot crashes loudly
on a 409 rather than looping quietly, so if the pod restarts with

```
Telegram 409 Conflict: another process is polling this bot token
```

something else is polling: a second replica, an old pod still terminating, or a
`trd bot serve` you left running on the host.

A webhook set on the bot suppresses `getUpdates` entirely — clear it with
`deleteWebhook` (see above) if the bot sees no messages at all.

### Which build is running

SB-443 was a day engine that never went flat, because the pod was executing code
from before the `session_close` rule existed. Every test passed; `main` was
correct; the only symptom was a position that did not close — indistinguishable
from "no rule fired". Nothing reported which code was executing.

So the engine states its provenance. Both surfaces carry it:

```bash
grep '"ev":"scan"' <(kubectl logs -n trd job/trd-day-scan-...) | jq .version
head -1 ~/.trd-day/status.txt     # trd engine — last scan ...  ·  build 0.1.0+0bc958d
```

The SHA is baked at image build (`--build-arg TRD_GIT_SHA=...`, set by
`scripts/deploy-k3s.sh` from `git rev-parse --short HEAD`), and the deploy prints
the old and new versions so a `--skip-build` no-op is visible rather than assumed.
A bare `0.1.0` with no `+sha` means the image was built by hand, outside the
script.

There is also a hard guard: if the stored config switches on a parameter whose
rule is missing from the build — `flat_at_minute` without `session_close` — the
scan refuses to run and says so. A day engine that quietly degrades into a swing
engine holds exactly the overnight risk its configuration forbids, so a stopped
engine is the better failure.

### How it fails

The secret is `optional: true`. With none configured the engine still scans and
logs that it sent nothing — trading never depends on the chat. A delivery failure
is warned about and swallowed: the trades are already recorded, and failing the
pass would make the next one re-evaluate a stale world.

Bot tokens never reach a log line — HTTP errors are re-raised without the URL
(there's a test for exactly that).

## Visibility

| Where | How |
|---|---|
| Phone | Telegram channel — a message per fill, with the reason |
| MacBook Air | `iCloud/trd/engine/status.txt`, or `trd restore` the published backup |
| Grafana | promtail tails the pod logs; every scan emits NDJSON, one event per line |
| Terminal | `kubectl logs -n trd -l app=trd --tail=100 -f` (all engines) or `-l component=trd-day` for one |
| Mini | `TRD_HOME=~/.trd-engine trd engine report` — same DB, via the hostPath |

### Reaching the Air: why the engine does not live in iCloud

The engine's database is **local to the mini**, not in iCloud, for two reasons:

1. A k3s pod runs in a Linux VM and cannot see `~/Library/Mobile Documents` —
   that is a macOS FileProvider path. DuckDB also needs real POSIX advisory
   locks, which do not survive that trip.
2. iCloud whole-file-syncs a binary and resolves conflicts by making duplicate
   copies, not by merging. Writing a DuckDB file every five minutes while a
   second Mac may also open it is the standard way to corrupt one.

Instead, each scan writes two small files next to the database, and a launchd job
on the host copies them into iCloud. It copies files only — it never opens the
database, so it can never contend with a scan.

```
pod  ──► ~/.trd-engine/status.txt          (positions + scorecard)
     └─► ~/.trd-engine/engine-backup.json  (full engine state + txns)
                │
   engine-publish.sh (launchd, every 5 min)
                ▼
        iCloud/trd/engine/
```

On the Air:

```bash
cat "$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd/engine/status.txt"

# or, for the full CLI against a local copy:
TRD_HOME=~/.trd-engine-view trd restore \
  "$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd/engine/engine-backup.json" --force
TRD_HOME=~/.trd-engine-view trd engine report
```

The backup carries stops, targets, ATR and trail high-water marks, so a restored
trade reads exactly like the original.

Install the publisher on the mini:

```bash
# edit USERNAME in the plist first
cp deploy/io.silverbeer.trd.enginepublish.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/io.silverbeer.trd.enginepublish.plist
launchctl start io.silverbeer.trd.enginepublish
cat ~/Library/Logs/trd-engine-publish.log
```

This is the one launchd agent that *does* belong alongside k3s — it schedules a
file copy, not a scan, so there is no second writer.

Event stream (`trd engine scan --ndjson`):

```json
{"ev":"close","ts":"...","symbol":"GOOGL","strategy":"pullback","rule":"stop","pnl":-79.68,"r_multiple":-1.05,"reason":"..."}
{"ev":"open","ts":"...","symbol":"GOOGL","strategy":"pullback","quantity":3,"price":326.56,"reason":"..."}
{"ev":"signal","ts":"...","symbol":"AAPL","strategy":"momentum","score":0.61,"acted":false,"reason":"..."}
{"ev":"scan","ts":"...","run_id":42,"scanned":10,"signals":1,"opened":1,"closed":1,"open_positions":2,"capacity":3}
```

Numbers are JSON numbers, not strings, so Grafana can graph them without a parse
step. Money is float **in the event stream only** — every stored value stays
`Decimal`.

## Design notes

**`concurrencyPolicy: Forbid` is load-bearing.** DuckDB is single-writer; two
overlapping scans would fight over the file lock.

**`backoffLimit: 0`.** A failed scan waits for the next tick rather than retrying
into a locked database. Five minutes away.

**Schedule is wider than the market.** cron cannot express "09:30–16:00", so the
CronJob runs `*/5 9-16` and the entrypoint trims the edges. `timeZone:
America/New_York` means DST needs no November edit.

**Re-scanning the same bar is safe.** A signal is stored once per `(symbol,
strategy, bar_date)` and stays a candidate only until acted on, so the 5-minute
cadence can never double-fill.

**hostPath, not a PVC.** The DuckDB file stays readable from the host, so
`TRD_HOME=~/.trd-engine trd engine report` works without kubectl. Edit the path
in `cronjob.yaml` for your machine.

## Troubleshooting

**`Permission denied` on the DuckDB file** — the hostPath mount maps to a
different uid than the container's `runAsUser: 1000`. Set `runAsUser` in
`cronjob.yaml` to the host's `id -u`.

**Pod can't see the hostPath at all** — k3s does not run natively on macOS. If
it is inside a VM (Rancher Desktop, Lima, colima), that VM must mount `/Users`.
If it does not, switch the volume to a `local-path` PVC and read the DB with
`kubectl exec` instead.

**Every job says "outside 09:30-16:00 ET"** — that is the guard working. Force
one run with `TRD_ENGINE_FORCE=1` (the `--test` flag does this).
