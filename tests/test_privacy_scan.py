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
