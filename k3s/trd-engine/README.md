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

### `~/.trd-engine` is not your real database

It is a separate, paper-only DuckDB holding one simulation account and ten
tickers' price history. Your real trd database is never opened by any of this —
the engine cannot reach it and does not need it, because it only trades paper.

Override the location with `ENGINE_HOME=/some/path ./scripts/deploy-k3s.sh`.

## Telegram feed

Fills — and only fills — get pushed. Scans are quiet the overwhelming majority
of the time; pushing every pass would train you to ignore the channel.

```bash
kubectl create secret generic trd-engine-telegram \
  --namespace trd \
  --from-literal=TELEGRAM_BOT_TOKEN='...' \
  --from-literal=TELEGRAM_CHAT_ID='...'
```

Token from @BotFather; chat id from `getUpdates` (see `secret.example.yaml`).
The secret is `optional: true` — with no secret the engine still scans and just
logs that it sent nothing. Notification failures never fail a scan: the trade is
already recorded, and failing the pass would make the next one re-evaluate a
stale world.

## Visibility

| Where | How |
|---|---|
| Phone | Telegram channel — a message per fill, with the reason |
| MacBook Air | `iCloud/trd/engine/status.txt`, or `trd restore` the published backup |
| Grafana | promtail tails the pod logs; every scan emits NDJSON, one event per line |
| Terminal | `kubectl logs -n trd -l app=trd --tail=100 -f` |
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
