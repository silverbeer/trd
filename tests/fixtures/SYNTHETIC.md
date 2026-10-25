# Synthetic fixtures

This repository is public. Every file under `tests/fixtures/` that is shaped
like a broker export must be listed here, or `scripts/privacy_scan.py` blocks it
(rules `export-header`, `export-footer`, and `forbidden-file` for a `.csv`).
Listing a file is a claim, reviewed like code: **it contains no real data** —
invented quantities, prices, dates and balances, public tickers only, and
placeholder account ids (`TEST-ACCT-*`). Never derive a fixture from a real
download by editing numbers; write it from the documented layout.

Every other rule (account numbers, home paths, private terms) still applies to
listed files.

- `tests/fixtures/activity/activity.txt` — synthetic: invented data, public tickers
- `tests/fixtures/activity/activity_later.txt` — synthetic: invented data, public tickers
- `tests/fixtures/activity/activity_mid_history.txt` — synthetic: invented data, public tickers
- `tests/fixtures/activity/activity_unknown.txt` — synthetic: invented data, public tickers
- `tests/fixtures/activity/positions.txt` — synthetic: invented data, public tickers
- `tests/fixtures/activity/positions_mid_history.txt` — synthetic: invented data, public tickers
- `tests/fixtures/activity/positions_mismatch.txt` — synthetic: invented data, public tickers
- `tests/fixtures/activity/positions_two_accounts.txt` — synthetic: invented data, public tickers
