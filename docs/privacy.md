# Privacy: real account data never enters this repo

This repository is **public**. trd tracks real retirement accounts. The rule
(SB-1421):

- **No real account data in the repo or its git metadata.** No broker export, no
  account number, no database, no `trd backup` file, no real home path, no
  figure or name the owner has listed as private. Commit messages, PR titles and
  bodies and branch names are git metadata too, and they outlive any scrub of the
  tree.
- **Telegram never says where money is held.** No brokerage name, no core
  money-market fund (each belongs to one brokerage), no account number, no
  private term.
- **Real data lives in `TRD_HOME` only** (`~/.trd-portfolio`, the engine homes).
  Tests use synthetic data: invented quantities, prices and balances, public
  tickers, placeholder account ids (`TEST-ACCT-*`).

Three layers enforce it. None of them prints a value it found: a finding shows
the rule id, the file and line, and a redacted preview — first and last
character and the length (`2…0 (6 chars)`).

## 1. Git hooks (local, every commit)

| Hook | Runs | Checks |
|---|---|---|
| `.githooks/pre-commit` | `privacy_scan.py --staged` | the ADDED lines of the staged diff, plus every staged file's name and content type |
| `.githooks/commit-msg` | `privacy_scan.py --message FILE` | the commit message (comment lines and the `--verbose` diff excluded) |
| `.githooks/pre-push` | `privacy_scan.py --text` | the name of every branch or tag being pushed |

Install on every machine, once per clone — git does not copy hook settings:

```bash
scripts/install-hooks.sh      # = git config core.hooksPath .githooks
```

The only bypass is `git commit --no-verify` / `git push --no-verify`. It is
possible and it is visible: CI runs the same scanner over the whole tree and the
PR text, so a bypassed commit still fails its PR.

## 2. CI (every push to main and every PR)

The `privacy` job in `.github/workflows/ci.yml` runs before anything is
installed (the scanner is stdlib-only and imports nothing from trd):

- `privacy_scan.py --tree` — every tracked file;
- on a pull request, the PR title, body and head branch through `--text`. They
  reach the shell through `env:`, never `${{ }}` inside `run:`, so a crafted
  title cannot inject a command.

CI has no private-terms file, so the `private-term` rule only runs locally.

## 3. Telegram send guard (every message)

`src/trd/notify/privacy.py`, called from `TelegramNotifier.send` and the bot's
`TelegramTransport.send` — the two places every Telegram message passes. A
message that breaks a rule is **refused whole** (fail closed) with a
`NotifyError` naming the rule, never the text. It is not edited: a guard that
rewrites text hides the renderer bug that produced it. Renderers are written so
legitimate messages pass — the IRA report says "a money market fund", never the
stored fund; live-engine alerts say "your broker app".

The shell alerts in `deploy/rh-lib.sh` / `deploy/live-runner.sh` call curl
directly and do **not** pass through this guard.

## The rules

| Rule id | What | Where it blocks |
|---|---|---|
| `export-header` | a broker export's column layout (compared with quotes and spaces removed, lowercased) | diff, tree |
| `export-footer` | a broker download's legal footer ("Brokerage services are provided by", "Date downloaded") | diff, tree |
| `account-number` | `[A-Z]\d{2}-\d{6}`; an 8–12 digit run within 20 characters of `acct`/`account` | everywhere, Telegram |
| `forbidden-file` | `*.duckdb*`, `*.db`, `*.sqlite*`, `*.ofx`, `*.qfx`, `*.xlsx`, `*.xls`, `*.pem`, `.env*` (except `.example`/`.sample`/`.template`), `*.csv` outside listed fixtures; a DuckDB or SQLite file by its bytes, whatever its name | diff, tree |
| `backup-json` | a JSON object with `version` and two or more of `trd backup`'s top-level keys (`accounts`, `transactions`, `instruments`, `cash_flows`, `alloc_plans`, `income`) | diff, tree |
| `home-path` | `/Users/<name>/` unless `<name>` is `USERNAME`, `CHANGE_ME`, `runner` or `Shared` | everywhere |
| `private-term` | a term from the private list (below) | everywhere, Telegram |
| `firm-name` | Fidelity, Robinhood, Schwab, Vanguard, E*Trade/ETRADE, SoFi, Merrill, Ameritrade, Interactive Brokers/IBKR, Webull, Public.com, Wealthfront, Betterment, Firstrade, Ally Invest | BLOCK in commit messages, PR text, branch names and Telegram; WARN in code |
| `money-market` | SPAXX, FDRXX, FZFXX, SPRXX, FZDXX, FCASH, SWVXX, VMFXX, VMRXX | BLOCK in commit messages, PR text, branch names and Telegram |

Placeholders that never count as an account number: `TEST-ACCT-*`,
`X00-000000`, `USERNAME`, `CHANGE_ME`, a run of one repeated digit (`00000000`)
and the start of `1234567890…`.

**Firm names in code only warn.** The code already uses them (`trd import
fidelity`, `docs/robinhood-mcp.md`, account names in examples); renaming is a
pending owner decision. `--tree` and long `--staged` runs print one summary line
for them instead of one line each.

**Casing in prose.** In a commit message, PR text or branch name, lowercase
`fidelity` is allowed — it is the ordinary word (`trd engine fidelity`, the
backtest-fidelity report, branch names cut from ticket titles). `Fidelity` and
`FIDELITY` are the firm and block. Every other firm blocks in any case. `SOFI`
in capitals is SoFi Technologies' ticker, a stock the engines may trade, and is
allowed. In Telegram all of them are case-insensitive except that ticker.

**Export layouts.** The two Fidelity layouts are the ones trd's importer reads.
The others (Schwab, Vanguard, Robinhood, E*Trade) were written from memory and
are best-effort: check them against a real export — locally, never committed —
and extend `EXPORT_HEADERS` in `scripts/privacy_scan.py`.

## Private terms

`~/.config/trd/private-terms.txt` — **not in the repo**. Override the path with
`TRD_PRIVATE_TERMS`. One term per line; `#` starts a comment. Matched
case-insensitively as whole tokens. A number also matches with or without
thousands separators: `1234567` finds `1,234,567`, `1_234_567` and
`1234567.00`.

```text
# names
Jane Example
# figures
1234567
```

Put in it what must never be public: real names, the former employer or plan
provider, account balances and planned amounts, the account's last four digits.
A missing file is a one-line notice, not a failure.

**The Telegram guard reads the same file.** A term there also refuses any
Telegram message containing it — so a figure your own reports print (a deposit
total the IRA report shows) will stop that report. List a figure only if no
message should ever carry it.

## Fixtures

Files under `tests/fixtures/` that look like a broker export must be listed in
[`tests/fixtures/SYNTHETIC.md`](../tests/fixtures/SYNTHETIC.md) (path in
backticks). Listing exempts a file from `export-header`, `export-footer` and the
CSV rule only; every other rule still applies. Listing is a claim, reviewed like
code: write a fixture from the documented layout, never by editing the numbers of
a real download.

## Exceptions

[`scripts/privacy_exceptions.txt`](../scripts/privacy_exceptions.txt), one per
line: `path[:line] rule reason`. A path without a line exempts that file for that
one rule; every other rule still applies. Prefer `path:line`. Adding a line is
reviewed like code and must say why the match is not real data. Current ones:
the importer that matches the footer, and the guardrail's own definitions,
tests and this document.

## Known history

Scrubbing the tree does not scrub history, and nothing here rewrites it. Audit
of `git log --all` at SB-1421 (264 commits); counts and commit ranges only —
no values:

- **The planned IRA rollover amount** appears in the diffs of 10 commits between
  `8033356` and `73d58c7`, and in 2 commit messages (`8033356`, `121d19c`). The
  initial-tranche figure derived from it appears in the same 10 commits. It was
  also in migration 029's header comment until SB-1422 rewrote the comment — a
  comment edit changes nothing the migration does, so the never-edit rule holds.
- **The cash balance used in the cash-yield tests** in 2 commits (`a12c71a`,
  `7ccec22`).
- **The prior plan's provider and fund name** (a test's deposit note) in 2
  commits (`8033356`, `121d19c`).
- **A real macOS username** in k3s `hostPath` placeholders in 17 commits between
  `fe67025` and `53f2388`. Never in a commit message.
- **Two 9-digit numbers used as broker account numbers** in
  `tests/test_rh_stream.py`, possibly real, in 2 commits (`186acf2`, `dafd03f`).
- **The live account's last four digits** in 2 commits (`03f7d5c`, `0568a84`);
  how much the live account was funded with and how much sits idle in 4 commits
  between `249e1c0` and `0568a84`.
- **Income figures and holdings of a real account** (examples in the glossary
  and a docstring) in 2 commits (`51af651`, `62fc80f`).
- **Brokerage names in commit messages**: 21 commits between `e0789d0` and
  `55012f4`. **Core money-market tickers in commit messages**: 2 commits
  (`a12c71a`, `7ccec22`).
- **Broker export column layouts**: 2 commits (`13e2cd2`, `55012f4`), the
  synthetic fixtures.
- No database, CSV, spreadsheet, `.env`, key or `trd backup` file has ever been
  committed.

Whether to rewrite history (and force-push a public repo) is an owner decision.

## Firm names in Telegram are redacted, not refused

A brokerage name in a message is usually a company being discussed — the bot's
`/add` reply names the issuer, the nightly AI review can mention one — not where
money is held. Refusing those would silently drop a whole review over a stock's
name, so `redact_firms` replaces the name with `[firm]` and the message still
goes. The rules that can identify an account — account-number shapes, private
terms, a custodian's money-market ticker — still refuse the send outright.
