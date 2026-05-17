"""Kill switches: hard halts on losses, manual kill file, kill env-var, connection loss."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..contracts import AccountState, RiskParams

logger = logging.getLogger(__name__)


def check_daily_loss(state: AccountState, params: RiskParams) -> tuple[bool, str]:
    """Trip when realized losses today exceed max_daily_loss_pct of equity."""
    if state.equity <= 0:
        return False, "non-positive equity"
    limit = -abs(params.max_daily_loss_pct) * state.equity
    if state.realized_pnl_today <= limit:
        return False, (
            f"daily loss limit hit: {state.realized_pnl_today} <= {limit}"
        )
    return True, "ok"


def check_kill_file(params: RiskParams) -> tuple[bool, str]:
    """Trip if the manual kill file exists on disk."""
    if Path(params.kill_file_path).exists():
        return False, f"kill file present at {params.kill_file_path}"
    return True, "ok"


def check_kill_env(params: RiskParams) -> tuple[bool, str]:
    """Trip if the kill env var is set to a truthy value.

    Truthy: "1", "true", "yes", "on" (case-insensitive). Empty/unset = ok.
    """
    raw = os.environ.get(params.kill_env_var, "")
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return False, f"kill env var {params.kill_env_var}={raw!r} is set"
    return True, "ok"


def check_connection(state: AccountState) -> tuple[bool, str]:
    if not state.connection_ok:
        return False, "broker connection down"
    return True, "ok"


def kill_switches_ok(state: AccountState, params: RiskParams) -> tuple[bool, str]:
    """Run every kill switch; return the first failure or (True, "ok").

    Order: connection → daily-loss → kill-file → kill-env. The connection check
    fires first because every other check assumes broker state is meaningful.
    """
    for fn in (
        lambda: check_connection(state),
        lambda: check_daily_loss(state, params),
        lambda: check_kill_file(params),
        lambda: check_kill_env(params),
    ):
        ok, reason = fn()
        if not ok:
            logger.warning("kill switch tripped: %s", reason)
            return False, reason
    return True, "ok"
