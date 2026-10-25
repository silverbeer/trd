"""scripts/privacy_scan.py, run the way the hooks and CI run it (SB-1421).

Every test drives the scanner as a subprocess against a throwaway git repo, so
what is pinned is the behaviour a commit actually meets: what blocks, what only
warns, what a listed synthetic fixture is allowed, and that a finding never
prints the value it found.

Values shaped like real data are assembled at runtime, so this file holds no
account-number or home-path shape for the tree scan to find in it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "scripts" / "privacy_scan.py"
ACCOUNT = "Z" + "41-" + "583920"
HOME = "/Users/" + "alicew" + "/.trd"
ACTIVITY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),"
    "Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)
POSITIONS_HEADER = '"Account Number","Account Name","Symbol","Description","Quantity","Last Price"'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    return root


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """The owner's real list must never decide a test, either way."""
    out = dict(os.environ)
    out["TRD_PRIVATE_TERMS"] = str(tmp_path / "absent.txt")
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        out[key] = value
    return out


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def stage(root: Path, path: str, content: str | bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    git(root, "add", "-f", path)


def scan(
    root: Path, env: dict[str, str], *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), *args],
        cwd=root,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


# ------------------------------------------------------------------ exports


def test_a_staged_broker_export_is_blocked(repo: Path, env) -> None:
    stage(repo, "data/history.csv", ACTIVITY_HEADER + "\n08/01/2025,YOU BOUGHT,VOO,,Cash\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[export-header] data/history.csv:1" in result.stderr
    assert "[forbidden-file] data/history.csv" in result.stderr


def test_an_export_pasted_under_any_name_is_blocked(repo: Path, env) -> None:
    stage(repo, "notes.md", f"see\n{POSITIONS_HEADER}\nDate downloaded 09/03/2025\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[export-header] notes.md:2" in result.stderr
    assert "[export-footer] notes.md:3" in result.stderr


def test_a_listed_synthetic_fixture_is_allowed_and_an_unlisted_one_is_not(repo: Path, env) -> None:
    body = ACTIVITY_HEADER + "\n08/01/2025,YOU BOUGHT,VOO,,Cash\n"
    stage(repo, "tests/fixtures/broker/activity.csv", body)
    assert scan(repo, env, "--staged").returncode == 1
    stage(
        repo,
        "tests/fixtures/SYNTHETIC.md",
        "- `tests/fixtures/broker/activity.csv` — synthetic: invented data, public tickers\n",
    )
    result = scan(repo, env, "--staged")
    assert result.returncode == 0, result.stderr
    stage(repo, "tests/fixtures/broker/other.txt", body)  # same content, not listed
    assert "[export-header] tests/fixtures/broker/other.txt" in scan(repo, env, "--staged").stderr


def test_a_listed_fixture_still_answers_to_every_other_rule(repo: Path, env) -> None:
    stage(repo, "tests/fixtures/SYNTHETIC.md", "- `tests/fixtures/a.txt` — synthetic\n")
    stage(repo, "tests/fixtures/a.txt", f"{ACTIVITY_HEADER}\nheld in {ACCOUNT}\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[account-number] tests/fixtures/a.txt:2" in result.stderr
    assert "export-header" not in result.stderr


# ------------------------------------------------------------------ files


@pytest.mark.parametrize(
    "path",
    [
        "trd.duckdb",
        "trd.duckdb.wal",
        "x.db",
        "x.sqlite",
        "s.ofx",
        "s.qfx",
        "s.xlsx",
        "k.pem",
        ".env",
    ],
)
def test_a_forbidden_file_is_blocked_by_name(repo: Path, env, path: str) -> None:
    stage(repo, path, "x\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1 and f"[forbidden-file] {path}" in result.stderr


def test_a_database_is_blocked_by_its_bytes_whatever_it_is_called(repo: Path, env) -> None:
    stage(repo, "notes.bin", b"\x00" * 8 + b"DUCK" + b"\x00" * 64)
    stage(repo, "other.bin", b"SQLite format 3\x00" + b"\x00" * 64)
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[forbidden-file] notes.bin: DuckDB" in result.stderr
    assert "[forbidden-file] other.bin: SQLite" in result.stderr


def test_a_trd_backup_is_blocked_by_its_shape(repo: Path, env) -> None:
    stage(repo, "snapshot.json", '{"version": 11, "accounts": [], "transactions": []}\n')
    stage(repo, "settings.json", '{"version": 2, "permissions": {}}\n')
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[backup-json] snapshot.json" in result.stderr
    assert "settings.json" not in result.stderr


def test_an_env_template_is_allowed(repo: Path, env) -> None:
    stage(repo, ".env.example", "TELEGRAM_BOT_TOKEN=CHANGE_ME\n")
    assert scan(repo, env, "--staged").returncode == 0


# ------------------------------------------------------------------ lines


def test_an_account_number_is_blocked_and_a_placeholder_is_not(repo: Path, env) -> None:
    stage(repo, "a.py", 'OK = ["TEST-ACCT-1", "X00-000000", "account 00000000"]\n')
    assert scan(repo, env, "--staged").returncode == 0
    stage(repo, "b.py", f'ACCT = "{ACCOUNT}"\nnote = "account number ' + "4819" + '37265"\n')
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[account-number] b.py:1" in result.stderr
    assert "[account-number] b.py:2" in result.stderr


def test_a_real_home_path_is_blocked_and_the_placeholder_is_not(repo: Path, env) -> None:
    stage(repo, "ok.yaml", "path: /Users/USERNAME/.trd-engine\n")
    assert scan(repo, env, "--staged").returncode == 0
    stage(repo, "bad.yaml", f"path: {HOME}\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1 and "[home-path] bad.yaml:1" in result.stderr


def test_only_added_lines_are_scanned(repo: Path, env) -> None:
    stage(repo, "a.py", f'ACCT = "{ACCOUNT}"\n')
    git(repo, "commit", "-q", "--no-verify", "-m", "seed", env=env)
    (repo / "a.py").write_text("ACCT = None\n", encoding="utf-8")
    git(repo, "add", "a.py")
    assert scan(repo, env, "--staged").returncode == 0  # removing it is the fix


def test_a_private_term_is_blocked_in_a_diff_and_a_message(repo: Path, env, tmp_path) -> None:
    terms = tmp_path / "terms.txt"
    terms.write_text("# mine\nAcme Pension\n4242424\n", encoding="utf-8")
    env = {**env, "TRD_PRIVATE_TERMS": str(terms)}
    stage(repo, "a.md", "rolled over from ACME PENSION\nworth $4,242,424.00\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[private-term] a.md:1" in result.stderr and "[private-term] a.md:2" in result.stderr
    message = tmp_path / "MSG"
    message.write_text("feat: track 4_242_424\n\n# comment lines are not scanned: Acme Pension\n")
    result = scan(repo, env, "--message", str(message))
    assert result.returncode == 1 and "[private-term] commit message:1" in result.stderr
    assert "commit message:3" not in result.stderr


def test_a_missing_private_list_is_a_notice_not_a_failure(repo: Path, env) -> None:
    result = scan(repo, env, "--text", stdin="nothing private here\n")
    assert result.returncode == 0
    assert "rule private-term skipped" in result.stderr


# ------------------------------------------------------------------ firm names


def test_a_firm_name_blocks_a_message_and_only_warns_in_code(repo: Path, env, tmp_path) -> None:
    message = tmp_path / "MSG"
    message.write_text("feat: import the Fidelity export\n")
    result = scan(repo, env, "--message", str(message))
    assert result.returncode == 1 and "BLOCK [firm-name] commit message:1" in result.stderr
    stage(repo, "importer.py", "BROKER = 'Robinhood'\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 0
    assert "WARN [firm-name] importer.py:1" in result.stderr


@pytest.mark.parametrize(
    ("text", "blocked"),
    [
        ("sb-1418-trd-import-fidelity", False),  # lowercase: the ordinary word
        ("fix: trd engine fidelity on 5m bars", False),
        ("FIDELITY positions", True),
        ("robinhood-mcp runbook", True),
        ("BUY SOFI on the breakout", False),  # the ticker, a stock
        ("moved cash to the sofi account", True),
        ("cash now in SPAXX", True),
    ],
)
def test_prose_rules(repo: Path, env, text: str, blocked: bool) -> None:
    result = scan(repo, env, "--text", "--label", "branch", stdin=text + "\n")
    assert (result.returncode == 1) is blocked, result.stderr


# ------------------------------------------------------------------ redaction


def test_a_finding_never_prints_the_value(repo: Path, env, tmp_path) -> None:
    terms = tmp_path / "terms.txt"
    terms.write_text("Zebulon Trust\n", encoding="utf-8")
    env = {**env, "TRD_PRIVATE_TERMS": str(terms)}
    stage(repo, "a.md", f"{ACCOUNT}\n{HOME}\nthe zebulon trust\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    out = result.stdout + result.stderr
    for value in (ACCOUNT, "alicew", "zebulon trust"):
        assert value.lower() not in out.lower(), out
    assert f"{ACCOUNT[0]}…{ACCOUNT[-1]} ({len(ACCOUNT)} chars)" in out


# ------------------------------------------------------------------ tree + exceptions


def test_the_tree_scan_honours_a_documented_exception(repo: Path, env) -> None:
    stage(repo, "old.sql", f"-- {ACCOUNT}\n")
    git(repo, "commit", "-q", "--no-verify", "-m", "seed", env=env)
    assert scan(repo, env, "--tree").returncode == 1
    stage(repo, "scripts/privacy_exceptions.txt", "old.sql:1 account-number applied, immutable\n")
    result = scan(repo, env, "--tree")
    assert result.returncode == 0, result.stderr
    stage(repo, "scripts/privacy_exceptions.txt", "old.sql:2 account-number wrong line\n")
    assert scan(repo, env, "--tree").returncode == 1


# ------------------------------------------------------------------ the hooks themselves


def test_the_hooks_block_a_commit_and_a_branch_name(repo: Path, env, tmp_path) -> None:
    shutil.copytree(REPO / ".githooks", repo / ".githooks")
    (repo / "scripts").mkdir()
    shutil.copy(SCANNER, repo / "scripts" / "privacy_scan.py")
    shutil.copy(REPO / "scripts" / "scan-commits.sh", repo / "scripts" / "scan-commits.sh")
    git(repo, "config", "core.hooksPath", ".githooks")

    stage(repo, "export.csv", ACTIVITY_HEADER + "\n")
    blocked = subprocess.run(
        ["git", "commit", "-q", "-m", "add data"], cwd=repo, capture_output=True, text=True, env=env
    )
    assert blocked.returncode != 0 and "[export-header] export.csv:1" in blocked.stderr
    git(repo, "rm", "-q", "--cached", "export.csv")

    stage(repo, "README.md", "hello\n")
    named = subprocess.run(
        ["git", "commit", "-q", "-m", "docs: Robinhood notes"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert named.returncode != 0 and "[firm-name] commit message:1" in named.stderr
    git(repo, "commit", "-q", "-m", "docs: readme", env=env)

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    push = subprocess.run(
        ["git", "push", "-q", "origin", "main:schwab-rollover"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert push.returncode != 0 and "[firm-name]" in push.stderr
    ok = subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=repo, capture_output=True, text=True, env=env
    )
    assert ok.returncode == 0, ok.stderr

    # SB-1428: a message that dodged commit-msg (--no-verify, another machine) is
    # caught on push, on a clean branch name, and the finding names the commit.
    git(repo, "checkout", "-q", "-b", "feat/clean")
    stage(repo, "README.md", "hello again\n")
    git(repo, "commit", "-q", "--no-verify", "-m", "docs: Schwab rollover notes", env=env)
    bad_push = subprocess.run(
        ["git", "push", "-q", "origin", "feat/clean"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert bad_push.returncode != 0 and "[firm-name] commit " in bad_push.stderr, bad_push.stderr
    git(repo, "commit", "-q", "--amend", "-m", "docs: more notes", env=env)
    good_push = subprocess.run(
        ["git", "push", "-q", "origin", "feat/clean"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert good_push.returncode == 0, good_push.stderr


def test_scan_commits_reads_every_message_in_a_range(repo: Path, env) -> None:
    script = REPO / "scripts" / "scan-commits.sh"
    (repo / "scripts").mkdir()
    shutil.copy(SCANNER, repo / "scripts" / "privacy_scan.py")
    stage(repo, "a.txt", "one\n")
    git(repo, "commit", "-q", "--no-verify", "-m", "one", env=env)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    stage(repo, "b.txt", "two\n")
    git(
        repo,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "two: moved cash to the Vanguard account",
        env=env,
    )
    stage(repo, "c.txt", "three\n")
    git(repo, "commit", "-q", "--no-verify", "-m", "three: fine", env=env)
    result = subprocess.run(
        ["sh", str(script), f"{base}..HEAD"], cwd=repo, capture_output=True, text=True, env=env
    )
    assert result.returncode == 1
    assert result.stderr.count("BLOCK [firm-name] commit ") == 1
    assert "Vanguard" not in result.stderr
    clean = subprocess.run(
        ["sh", str(script), f"{base}..{base}"], cwd=repo, capture_output=True, text=True, env=env
    )
    assert clean.returncode == 0 and "no commits in range" in clean.stderr


# ------------------------------------------------------------------ SB-1429: shapes that got past


def terms_env(env, tmp_path, *terms: str) -> dict[str, str]:
    path = tmp_path / "terms.txt"
    path.write_text("\n".join(terms) + "\n", encoding="utf-8")
    return {**env, "TRD_PRIVATE_TERMS": str(path)}


@pytest.mark.parametrize(
    "text",
    [
        "jane-example",  # slug
        "jane_example",  # snake
        "janeexample",  # concatenated
        "Jane​Example",  # zero-width space inside
        "Jane­Example",  # soft hyphen inside
        "JANE  EXAMPLE",  # double space, upper
        "maplewoodira",  # a one-word term inside a longer token
        "the maplewood-ira plan",
    ],
)
def test_a_private_term_is_found_in_every_spelling(repo: Path, env, tmp_path, text: str) -> None:
    env = terms_env(env, tmp_path, "Jane Example", "maplewood")
    stage(repo, "a.md", f"note: {text}\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1 and "[private-term] a.md:1" in result.stderr, result.stderr
    assert "jane" not in result.stderr.lower() and "maplewood" not in result.stderr.lower()


def test_a_short_term_still_has_to_stand_alone(repo: Path, env, tmp_path) -> None:
    env = terms_env(env, tmp_path, "main")
    stage(repo, "a.py", "domain = remain\n")
    assert scan(repo, env, "--staged").returncode == 0
    stage(repo, "b.py", "branch = 'main'\n")
    assert scan(repo, env, "--staged").returncode == 1


def test_a_term_split_across_two_lines_is_found_once(repo: Path, env, tmp_path) -> None:
    env = terms_env(env, tmp_path, "Jane Example", "maplewood")
    stage(repo, "a.md", "paid to Jane\nExample on Monday\nmaple\nwood\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[private-term] a.md:1: " in result.stderr and "split across lines" in result.stderr
    assert "[private-term] a.md:3: " in result.stderr
    assert result.stderr.count("[private-term]") == 2
    stage(repo, "c.py", 'acct = ("maple"\n        "wood")\n')  # a string continued in code
    assert "[private-term] c.py:1: " in scan(repo, env, "--staged").stderr
    stage(repo, "b.md", "paid to Jane Example\n")  # a whole-line hit is reported once, not twice
    result = scan(repo, env, "--staged")
    assert result.stderr.count("[private-term] b.md") == 1


def test_a_private_term_in_a_file_name_is_blocked(repo: Path, env, tmp_path) -> None:
    env = terms_env(env, tmp_path, "Jane Example")
    stage(repo, "notes/jane-example-2026.md", "nothing here\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[private-term] notes/jane-example-2026.md: " in result.stderr
    # the location names the file, which is the term; the preview never does
    assert "j…e (12 chars), in the file name" in result.stderr
    git(repo, "commit", "-q", "--no-verify", "-m", "seed", env=env)
    assert "in the file name" in scan(repo, env, "--tree").stderr


def test_a_binary_file_is_blocked_and_its_strings_are_read(repo: Path, env, tmp_path) -> None:
    env = terms_env(env, tmp_path, "Jane Example")
    stage(repo, "blob.bin", b"\x00\x01\x02" * 8 + b"paid to Jane Example" + b"\x00" * 16)
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[forbidden-file] blob.bin: binary file" in result.stderr
    assert "[private-term] blob.bin:binary" in result.stderr
    git(repo, "rm", "-q", "--cached", "blob.bin")
    stage(repo, "logo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # an allowed asset type
    assert scan(repo, env, "--staged").returncode == 0
    stage(repo, "logo2.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"Jane Example" + b"\x00" * 8)
    assert "[private-term] logo2.png:binary" in scan(repo, env, "--staged").stderr


def glue(*parts: str) -> str:
    """Shapes assembled at runtime so this file never holds one for the tree scan."""
    return "".join(parts)


@pytest.mark.parametrize(
    ("text", "blocked"),
    [
        (glue("account ", "4821", "-3579", "-01"), True),
        (glue("account ", "4821", " 3579", " 01"), True),
        (glue("4821", " 3579", " 0142", " is the acct"), True),
        (glue("ending ", "****", "4821"), True),
        (glue("card ", "xxxx", "4821"), True),
        (glue("acct ", "...", "4821"), True),
        (glue("account ", "X", "47182"), True),
        (glue("Z47", " ", "182936"), True),  # letter-dd, space, dddddd
        ("account opened 2026-10-25", False),  # a date
        ("on 10-25-2026 the account", False),
        ("account 12 34", False),  # too short to be one
        ("**2026 goals** for the account", False),  # markdown bold, not a mask
        ("see account docs in ~/.trd", False),
    ],
)
def test_account_number_shapes(repo: Path, env, text: str, blocked: bool) -> None:
    stage(repo, "a.md", text + "\n")
    result = scan(repo, env, "--staged")
    assert (result.returncode == 1) is blocked, result.stderr
    if blocked:
        assert "[account-number] a.md:1" in result.stderr


@pytest.mark.parametrize(
    ("text", "blocked"),
    [
        (glue("/home/", "alicew", "/.trd"), True),
        (glue("~", "alicew", "/.trd-engine"), True),
        ("/home/runner/work/trd", False),
        ("~/.trd", False),
        ("~5 minutes, ~ten names", False),
        ("/Users/USERNAME/.trd", False),
    ],
)
def test_home_path_shapes(repo: Path, env, text: str, blocked: bool) -> None:
    stage(repo, "a.yaml", f"path: {text}\n")
    result = scan(repo, env, "--staged")
    assert (result.returncode == 1) is blocked, result.stderr
    findings = [line for line in result.stderr.splitlines() if "note:" not in line]
    assert "alicew" not in "\n".join(findings)  # the notice names tmp_path, i.e. this test


@pytest.mark.parametrize(
    "path",
    [
        ".envrc",
        "deploy/live.envrc",
        "prod.env",
        "a.tsv",
        "a.jsonl",
        "a.qif",
        "a.numbers",
        "a.ods",
        "a.zip",
        "id.key",
        "a.p12",
    ],
)
def test_more_forbidden_files_are_blocked_by_name(repo: Path, env, path: str) -> None:
    stage(repo, path, "x\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1 and f"[forbidden-file] {path}" in result.stderr


def test_an_env_named_file_with_a_template_suffix_is_allowed(repo: Path, env) -> None:
    stage(repo, "prod.env.example", "TOKEN=CHANGE_ME\n")
    assert scan(repo, env, "--staged").returncode == 0


def test_archives_keys_and_backups_are_blocked_by_content(repo: Path, env) -> None:
    stage(repo, "sheet.bin", b"PK\x03\x04" + b"\x00" * 32)  # xlsx / numbers / zip renamed
    stage(
        repo,
        "id_ed25519",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n",
    )
    stage(repo, "one.json", '{"accounts": [{"id": "TEST-ACCT-1"}]}\n')  # one key, no version
    stage(repo, "two.json", '{"version": 9, "watchlists": []}\n')
    stage(repo, "ok.json", '{"version": 2, "accounts": "none", "permissions": {}}\n')
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[forbidden-file] sheet.bin: zip archive" in result.stderr
    assert "[forbidden-file] id_ed25519: private key" in result.stderr
    assert "[backup-json] one.json" in result.stderr and "[backup-json] two.json" in result.stderr
    assert "ok.json" not in result.stderr


def test_a_table_pasted_into_a_text_file_is_blocked(repo: Path, env) -> None:
    rows = "\n".join(f"2025-0{i},buy,VOO,{i}0,500.0{i},0" for i in range(1, 6)) + "\n"
    stage(repo, "data.txt", "symbol,side,qty,price,fees,note\n" + rows)
    stage(repo, "notes.txt", "a, b and c\nmore prose, with a comma\nand, another, one, here\n")
    stage(
        repo, "two.txt", "date,account,symbol,side,quantity,price\n2026-01-01,main,SPY,buy,1,500\n"
    )
    result = scan(repo, env, "--staged")
    assert result.returncode == 1
    assert "[delimited-text] data.txt" in result.stderr and "notes.txt" not in result.stderr
    assert "[delimited-text] two.txt" in result.stderr  # a header and one row is already a table
    stage(repo, "tests/fixtures/SYNTHETIC.md", "- `tests/fixtures/t/rows.txt` — synthetic\n")
    stage(repo, "tests/fixtures/t/rows.txt", "symbol,side,qty,price,fees,note\n" + rows)
    assert "rows.txt" not in scan(repo, env, "--staged").stderr


def test_an_export_row_without_its_header_is_blocked(repo: Path, env) -> None:
    date = glue("08/0", "1/2025")
    row = ",".join(
        [date, "YOU BOUGHT", "VOO", "ACME S&P 500 ETF", "Cash", "500.12", "10", "0", "0"]
    )
    stage(repo, "notes.md", f"history:\n{row}\n")
    result = scan(repo, env, "--staged")
    assert result.returncode == 1 and "[export-row] notes.md:2" in result.stderr
    stage(
        repo,
        "doc.md",
        "YOU BOUGHT / REINVESTMENT = buy, YOU SOLD = sell, DIVIDEND RECEIVED = income\n",
    )
    assert "doc.md" not in scan(repo, env, "--staged").stderr
