import os
from pathlib import Path

from pydantic import BaseModel

DEFAULT_ACCOUNT = "main"


class Settings(BaseModel):
    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / "trd.duckdb"


def get_settings() -> Settings:
    home = Path(os.environ.get("TRD_HOME", str(Path.home() / ".trd")))
    return Settings(home=home)
