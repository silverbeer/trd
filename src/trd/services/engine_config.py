"""Show and change a running engine's exit params (SB-1824).

`trd engine init` builds a config; it was never a way to retune one, and doing
so rebuilds the engine around the change. Before this, the only route to a
different trailing stop was hand SQL — and a knob nobody can turn from the CLI
is a knob that does not get measured, changed and audited.

What changes, deliberately, is ONE thing: the `exit_params` JSON bag on the
engine's existing config row, in place. The id, account, universe, strategies,
capital and mode are untouched, and there is no migration: every build reads
the same bag, so a runner still on an older build is not handed a schema it
does not know (mixed builds on one DuckDB home is what corrupted the live DB on
2026-11-24, SB-1773).

Every change appends one line per key to `TRD_HOME/engine-config-changes.jsonl`
— the reason is required, because "why is the trail 1.5?" is exactly the
question someone asks in a month.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import duckdb
from pydantic import BaseModel

from trd.engine.exits import DEFAULT_EXIT_PARAMS
from trd.errors import TrdError
from trd.models import EngineConfig
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo
from trd.timeframes import day_mode_on_daily_bars

AUDIT_FILENAME = "engine-config-changes.jsonl"


class ParamScope(StrEnum):
    """What a change to a param reaches."""

    # Read on every pass against every open position: the next scan uses it.
    OPEN = "open positions"
    # Fixed on the position at entry (stop and target prices are stored, and
    # the initial stop is immutable so closed-trade R stays meaningful).
    NEW = "new entries only"
    # Gates on whether to buy at all; nothing held is affected.
    ENTRY = "entry gate"
    # The live scan never reads it. Setting one would claim a change that
    # cannot happen, so `set` refuses.
    BACKTEST = "backtest only"


SCOPES: dict[str, ParamScope] = {
    "stop_atr_mult": ParamScope.NEW,
    "target_r": ParamScope.NEW,
    "trail_atr_mult": ParamScope.OPEN,
    "trail_activate_r": ParamScope.OPEN,
    "max_sessions": ParamScope.OPEN,
    "scale_out_pct": ParamScope.OPEN,
    "rsi_exit": ParamScope.OPEN,
    "indicator_grace_sessions": ParamScope.OPEN,
    "flat_at_minute": ParamScope.OPEN,
    "entry_delay_minutes": ParamScope.ENTRY,
    "regime_sma": ParamScope.ENTRY,
    "regime_vix_max": ParamScope.ENTRY,
    "rotate_min_score": ParamScope.BACKTEST,
    "max_per_symbol": ParamScope.BACKTEST,
}
# A zero here is not "off" — it is a stop at the entry price, a target at the
# entry price, or a trail glued to the high.
MUST_BE_POSITIVE = frozenset({"stop_atr_mult", "target_r", "trail_atr_mult"})


class ExitParam(BaseModel):
    key: str
    value: float
    default: float
    changed: bool
    scope: ParamScope


class ConfigView(BaseModel):
    config_id: int
    mode: str
    timeframe: str
    params: list[ExitParam]


class ParamChange(BaseModel):
    key: str
    old: float
    new: float
    scope: ParamScope


class ConfigChange(BaseModel):
    changes: list[ParamChange]
    reason: str
    at: datetime
    # Symbols of the open positions an OPEN-scope change reaches on the next scan.
    open_positions: list[str]
    audit_path: str | None


def _view(config: EngineConfig) -> ConfigView:
    params = {**DEFAULT_EXIT_PARAMS, **config.exit_params}
    return ConfigView(
        config_id=config.id,
        mode=config.mode.value,
        timeframe=config.timeframe,
        params=[
            ExitParam(
                key=key,
                value=float(params[key]),
                default=float(default),
                changed=float(params[key]) != float(default),
                scope=SCOPES.get(key, ParamScope.OPEN),
            )
            for key, default in DEFAULT_EXIT_PARAMS.items()
        ],
    )


class EngineConfigService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.configs = EngineConfigRepo(conn)
        self.positions = EnginePositionRepo(conn)

    def _config(self) -> EngineConfig:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured in this TRD_HOME.")
        return config

    def show(self) -> ConfigView:
        return _view(self._config())

    def set(
        self,
        values: dict[str, float],
        reason: str,
        now: datetime,
        audit_dir: Path | None = None,
    ) -> ConfigChange:
        """Change exit params in place. Every refusal happens before any write."""
        if not values:
            raise TrdError("Nothing to set: pass -p KEY=VALUE.")
        if not reason or not reason.strip():
            raise TrdError("A --reason is required: say why, for whoever reads this later.")
        unknown = sorted(k for k in values if k not in DEFAULT_EXIT_PARAMS)
        if unknown:
            settable = sorted(
                k for k in DEFAULT_EXIT_PARAMS if SCOPES.get(k) != ParamScope.BACKTEST
            )
            raise TrdError(
                f"Unknown exit params: {', '.join(unknown)}. Settable: {', '.join(settable)}."
            )
        backtest_only = sorted(k for k in values if SCOPES.get(k) == ParamScope.BACKTEST)
        if backtest_only:
            raise TrdError(
                f"{', '.join(backtest_only)}: backtest only — the live scan never reads it, "
                "so setting it here would change nothing. Measure it with 'trd engine backtest -p'."
            )
        for key, value in values.items():
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise TrdError(f"{key} must be a finite number >= 0, got {value!r}.")
            if key in MUST_BE_POSITIVE and number == 0:
                raise TrdError(f"{key} must be greater than 0.")

        config = self._config()
        merged = {**config.exit_params, **{k: float(v) for k, v in values.items()}}
        flat_at = int(merged.get("flat_at_minute", 0))
        if config.is_live and flat_at:
            raise TrdError("A live engine has no flat-at time: it is a swing engine on daily bars.")
        refused = day_mode_on_daily_bars(config.timeframe, flat_at)
        if refused:
            raise TrdError(refused)

        current = {**DEFAULT_EXIT_PARAMS, **config.exit_params}
        changes = [
            ParamChange(
                key=key,
                old=float(current[key]),
                new=float(value),
                scope=SCOPES.get(key, ParamScope.OPEN),
            )
            for key, value in values.items()
            if float(current[key]) != float(value)
        ]
        open_syms: list[str] = []
        if any(c.scope == ParamScope.OPEN for c in changes):
            open_syms = sorted(
                inst.symbol for _pos, inst in self.positions.list_open(config.account_id)
            )
        if not changes:
            return ConfigChange(
                changes=[], reason=reason, at=now, open_positions=open_syms, audit_path=None
            )

        self.conn.execute(
            "UPDATE engine_config SET exit_params = ? WHERE id = ?",
            [json.dumps(merged), config.id],
        )
        audit_path: Path | None = None
        if audit_dir is not None:
            audit_path = audit_dir / AUDIT_FILENAME
            with audit_path.open("a", encoding="utf-8") as fh:
                for c in changes:
                    fh.write(
                        json.dumps(
                            {
                                "at": now.isoformat(),
                                "config_id": config.id,
                                "key": c.key,
                                "old": c.old,
                                "new": c.new,
                                "reason": reason.strip(),
                            }
                        )
                        + "\n"
                    )
        return ConfigChange(
            changes=changes,
            reason=reason.strip(),
            at=now,
            open_positions=open_syms,
            audit_path=str(audit_path) if audit_path else None,
        )
