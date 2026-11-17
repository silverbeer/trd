# Monthly IRA upkeep

Once a month, after the month has ended: bring the managed retirement accounts up to
date from the custodian's exports, record the self-managed account's cash interest,
then send the head-to-head (`trd alloc compare`, SB-1420).

The custodian has no retail API, so the managed accounts are only as current as the
last import. The report knows that: a managed account whose last import is older than
the month's end is shown with ⏳ and left out of the verdict.

Real account names, numbers and the exports themselves never enter this repo — see
[privacy.md](privacy.md). The names live in your shell or `~/.config/trd/live.env`.

## When

The **first business day** of the month, or any day after it. An import made before
the month ended doesn't count as fresh for that month.

## What you need: 4 files

For **each** managed account, one at a time, from the custodian's website:

| Export | Range | Why |
|---|---|---|
| **Activity History** | from about two weeks **before** the last import, through **today** | every buy, sell, dividend, reinvestment and advisory fee since the last import |
| **Positions** | today | proves the import: every holding and the cash must match to the share |

Select the single account before downloading, never "all accounts". The Activity
export does not name its account, so download one account's pair, then the other's,
and don't mix them up. The Positions export carries the account number in its first
column; only the last four digits are needed to tell which file is which.

**The Activity range must reach the Positions date.** If it stops short, the import's
check shows a cash difference. A quarterly advisory fee missing from the range looks
exactly like that (SB-1733).

Overlapping a previous file is safe: every row is keyed, so a row already imported
is never written twice.

## Steps

### 1. Import each managed account

Dry run first, then the real import. The account name is the one the account was
first imported under:

```bash
trd import activity ACTIVITY.csv --positions POSITIONS.csv \
  --account "<initials> Managed IRA" --dry-run
```

Read the end of the output:

- `positions match`: every holding and the cash agree. Run it again without
  `--dry-run`.
- `positions DO NOT match`: don't import. See *When it doesn't match* below.

The listing of advisory fees on every run is deliberate: check the new quarter's fee
is there.

`--opening-from-positions` and `--create` are for the **first** import of an account
only. Never pass them again.

### 2. Self-managed account: the cash fund

The self-managed account has no export to import. Two entries, from its monthly
statement:

```bash
# last month's interest from the core money-market fund
trd income add AMOUNT --account "<self-managed account>" --kind cash_sweep --date YYYY-MM-DD

# the fund's current yield, if it changed
trd alloc edit --account "<self-managed account>" --cash-yield RATE
```

### 3. Send the report

```bash
trd alloc compare --notify      # last month; accounts from TRD_IRA_COMPARE
```

`TRD_IRA_COMPARE` is `"NAME=LABEL;NAME=LABEL;..."`. Only the labels are printed or
sent. Without `--notify` it prints to the terminal only; `--month YYYY-MM` reports a
different month.

## When it doesn't match

| Symptom | Cause | Fix |
|---|---|---|
| Cash off, every holding matches | Activity range stops before the Positions date | download Activity again through today |
| A holding off | a row the export left out, or a trade that settled after the download | download both files again, at the same time |
| "sells shares trd has no record of" | Activity starts before anything trd knows, on an account never imported | first import only: `--opening-from-positions` |
| "unknown action" | the custodian used a phrase the importer doesn't know | nothing is written; the importer needs the new action (file a ticket) |
| Report shows ⏳ for an account | its last import is older than the month's end | import it, then send again |

## Afterwards

Leave the exports in your Downloads folder or delete them. Never move them into a
repository: `*.csv` is ignored here, and the privacy hook blocks export-shaped text
anywhere else.
