import os
from pathlib import Path

from pydantic import BaseModel

DEFAULT_ACCOUNT = "main"
# The account's core money-market fund(s), one ticker per line, `#` comments.
# Which tickers those are names the custodian, so the list lives in the home
# (or in TRD_CORE_SYMBOLS, comma-separated), never in this repository.
CORE_SYMBOLS_FILE = "core-symbols.txt"


class Settings(BaseModel):
    home: Path
    core_symbols: frozenset[str] = frozenset()

    @property
    def db_path(self) -> Path:
        return self.home / "trd.duckdb"

    @property
    def core_symbols_path(self) -> Path:
        return self.home / CORE_SYMBOLS_FILE


def _core_symbols(home: Path) -> frozenset[str]:
    out = {
        s.strip().upper() for s in os.environ.get("TRD_CORE_SYMBOLS", "").split(",") if s.strip()
    }
    path = home / CORE_SYMBOLS_FILE
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line.upper())
    return frozenset(out)


def get_settings() -> Settings:
    home = Path(os.environ.get("TRD_HOME", str(Path.home() / ".trd")))
    return Settings(home=home, core_symbols=_core_symbols(home))
