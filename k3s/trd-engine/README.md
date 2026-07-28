# trd engine on k3s

One `trd engine scan` every 5 minutes during the regular session, as a CronJob.
Replaces the launchd agent in `deploy/` — **do not run both against the same
database**, DuckDB allows one writer.

## Deploy

```bash
cd ~/gitrepos/trd && git pull
./scripts/deploy-k3s.sh --test     # build, import, apply, then run one scan now
```

The script prints the target context and waits for confirmation — the engine
writes trades, so deploying to the wrong cluster is not a no-op.

First run needs history (the rules want 200 bars). From the host:

```bash
TRD_HOME=~/.trd-engine trd init
TRD_HOME=~/.trd-engine trd engine init
TRD_HOME=~/.trd-engine trd sync --full
```

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
| Phone / Air | Telegram channel — a message per fill, with the reason |
| Grafana | promtail tails the pod logs; every scan emits NDJSON, one event per line |
| Terminal | `kubectl logs -n trd -l app=trd --tail=100 -f` |
| Host | `TRD_HOME=~/.trd-engine trd engine report` — same DB, via the hostPath |

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
