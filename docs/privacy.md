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
| `.githooks/pre-push` | `privacy_scan.py --text`, `scan-commits.sh` | the name of every branch or tag being pushed, and the message of every commit the remote does not have yet |

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
  title cannot inject a command;
- on a pull request, the message of every commit in the PR
  (`scripts/scan-commits.sh base..head`); on a push to main, the messages
  pushed. A branch commit that dodged the local hooks is caught here, and so
  is a squash whose message was edited in the merge dialog.

**The private-terms list in CI** comes only from the repository secret
`TRD_PRIVATE_TERMS_B64` (the file, base64). Setting it is an owner decision:
the terms leave this machine, encrypted at rest and masked in logs, and the
scanner prints a rule and a location, never a value. Without the secret the
`private-term` rule is skipped with a notice and every shape rule still runs.

**Repository settings** (`scripts/repo-settings.sh`, idempotent): squash and
merge commits carry the PR title only (`BLANK` message), because the merge
dialog's text is editable by hand and nothing scans the result; `privacy` and
`test` are both required checks on `main`; admins are not exempt. Branch
protection needs the repo public or a paid plan.

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
| `account-number` | `[A-Z]\d{2}-\d{6}` (dash or space); an 8–12 digit run, plain or grouped by spaces or dashes (`NNNN-NNNN-NN`), within 20 characters of `acct`/`account`; a masked last-four (`****NNNN`, `xxxxNNNN`, `...NNNN`), and `XNNNNN` near the keyword. A date is never one | everywhere, Telegram |
| `forbidden-file` | `*.duckdb*`, `*.db`, `*.sqlite*`, `*.ofx`, `*.qfx`, `*.qif`, `*.xlsx`, `*.xls`, `*.xlsm`, `*.numbers`, `*.ods`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.ppk`, `*.jsonl`, `*.parquet`, `*.feather`, `*.pkl`, archives (`*.zip`, `*.7z`, `*.tar`, `*.gz`, …); `.env*`, `*.env` and `.envrc` (except `.example`/`.sample`/`.template`); `*.csv` and `*.tsv` outside listed fixtures; by content whatever the name: a DuckDB or SQLite file, a zip/Office/Numbers document, a PEM/OpenSSH/PuTTY private key; any binary file that is not an image, font or PDF (and the printable strings inside every binary are scanned like text) | diff, tree |
| `backup-json` | a JSON object with any of `trd backup`'s top-level keys (`accounts`, `transactions`, `instruments`, `cash_flows`, `alloc_plans`, `income`, `plans`, `watchlists`, `earnings_results`) holding a list — one table is still a backup | diff, tree |
| `delimited-text` | a `.txt`, `.dat`, `.log`, `.tsv` or suffix-less file with two or more consecutive lines carrying the same count of 4+ commas or tabs: a table pasted under a harmless name. Listed synthetic fixtures are exempt | diff, tree |
| `export-row` | a data row of an activity export with its header and footer cut off: one of the broker's action phrases (`YOU BOUGHT`, `DIVIDEND RECEIVED`, …) with a US date and six or more commas on one line | diff, tree |
| `home-path` | `/Users/<name>`, `/home/<name>` and `~<name>/` unless `<name>` is `USERNAME`, `CHANGE_ME`, `runner`, `Shared`, `user`, `ubuntu` | everywhere |
| `private-term` | a term from the private list (below), in text, in a file name, inside a binary, in any spelling (see below) | everywhere, Telegram |
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
`TRD_PRIVATE_TERMS`. One term per line; `#` starts a comment. A number matches
with or without thousands separators: `1234567` finds `1,234,567`, `1_234_567`
and `1234567.00`. A word term is matched against the FOLDED text — NFKC,
zero-width and other format characters removed, lowercased — with any of space,
`-`, `_` or `.` between its words, so `Jane Example` finds `jane-example`,
`jane_example`, `janeexample` and `Jane\u200bExample`. A term of six characters
or more also matches inside a longer token (`maplewood` finds `maplewoodira`);
a shorter one must stand alone, or `main` would hit `domain`. Two consecutive
lines are also read as one, so a term split across a line break is found and
reported at the first line. File names and the printable strings inside binary
files are matched the same way.

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
of `git log --all` at SB-1421, re-measured 2026-10-24 for the repo privacy
audit (266 commits); counts and commit ranges only — no values:

- **The planned IRA rollover amount** appears in the diffs of 12 commits between
  `8033356` and `73d58c7`, and in 2 commit messages (`8033356`, `121d19c`). The
  initial-tranche figure derived from it appears in 8 of them. It was
  also in migration 029's header comment until SB-1422 rewrote the comment — a
  comment edit changes nothing the migration does, so the never-edit rule holds.
- **The cash balance used in the cash-yield tests** in 2 commits (`a12c71a`,
  `7ccec22`).
- **The prior plan's provider and fund name** (a test's deposit note) in 2
  commits (`8033356`, `121d19c`).
- **A real macOS username** in k3s `hostPath` placeholders in 19 commits between
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
- **Pull request bodies** on GitHub carried real figures (a deposit, worth,
  gain, milestone amounts, portfolio values, a broker cash balance) and a real
  account name in at least ten PRs. PR text is GitHub metadata, not git: a
  history rewrite does not touch it, and GitHub keeps a body's edit history.
- **Deleted branches are still public.** GitHub keeps every PR's head commit
  reachable at `refs/pull/N/head`, so a branch commit and its squash both
  stay readable after the branch is deleted.

The owner's decision (2026-10-24, audit report kept off-repo): rewrite history
in a throwaway clone with `git filter-repo`, then delete and re-create the
GitHub repository rather than scrub PR bodies one by one — the only way to
drop the PR bodies, their edit history and the `refs/pull/*` commits together.
Re-run `scripts/repo-settings.sh` on the new repository.

## Firm names in Telegram are redacted, not refused

A brokerage name in a message is usually a company being discussed — the bot's
`/add` reply names the issuer, the nightly AI review can mention one — not where
money is held. Refusing those would silently drop a whole review over a stock's
name, so `redact_firms` replaces the name with `[firm]` and the message still
goes. The rules that can identify an account — account-number shapes, private
terms, a custodian's money-market ticker — still refuse the send outright.
