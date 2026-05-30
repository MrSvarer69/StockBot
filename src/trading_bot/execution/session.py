"""Live-paper trading run loop.

  poll  →  strategy  →  risk  →  order

Every order placement path passes through `risk.validate_order`. The loop
maintains the per-day counters that the risk caps depend on
(`cumulative_notional_today`, `realized_pnl_today`).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections.abc import Mapping
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from ..contracts import (
    AccountState,
    OrderSide,
    OrderType,
    Position,
    ProposedOrder,
    RiskParams,
    Trade,
)
from ..ops.trade_table import (
    render_summary_line,
    render_trade_event,
    render_trade_table,
)
from ..risk import ratchet_stop, size_position, trail_offset, validate_order
from ..risk.kill_switch import check_daily_loss, check_kill_env, check_kill_file
from ..strategy.base import Strategy
from ..strategy.picker import rank_entry_signals
from .broker import (
    BrokerAuthError,
    BrokerClient,
    BrokerError,
    BrokerOrderResponse,
    BrokerValidationError,
)

logger = logging.getLogger(__name__)

# Default locations for ops state. Tests monkey-patch these via the
# tests/test_execution/conftest.py autouse fixture to keep writes out of the
# repo's data/ directory.
_DEFAULT_SESSION_STATE_DIR = Path("data/ops/session_state")
_DEFAULT_UNRECONCILED_DIR = Path("data/ops/unreconciled")
_DEFAULT_CARRIED_DIR = Path("data/ops/carried")

# A carried-position record older than this many calendar days is treated as
# stale and ignored (positions fall back to the orphan refusal). 4 days covers
# a Friday-close carry over a long weekend (e.g. holiday Monday) into Tuesday.
_CARRIED_MAX_AGE_DAYS = 4

# Tolerance on the recorded vs broker avg-entry-price when matching a carried
# position. Corroboration only — a gross mismatch means a different position
# and must refuse. Not used to size anything (broker qty is the authority).
_CARRIED_PRICE_TOLERANCE_PCT = Decimal("0.005")

# Kill-switch poll cadence inside `_sleep_with_safety_checks`. With a 60s
# poll interval, this caps worst-case kill-file/env detection at ~5s.
_KILL_CHECK_CHUNK_SECONDS = 5

# Daily-loss check runs every Nth chunk inside the sleep helper (≈15s at a
# 5s chunk). Cheaper than per-chunk because it costs one broker.get_account.
_DAILY_LOSS_CHECK_EVERY_N_CHUNKS = 3

# Market-order fill-poll: after submit/close, poll the broker up to this many
# times at this interval before giving up and writing the trade record with a
# null fill price. Paper market orders typically fill in <1s; the cap keeps the
# run loop from blocking on a stuck order.
_FILL_POLL_INTERVAL_SECONDS = 0.5
_FILL_POLL_MAX_ATTEMPTS = 6  # ~3s total

# Terminal order statuses where further polling is pointless.
_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "rejected", "canceled", "cancelled", "expired", "done_for_day"}
)

# Tamper-resistance threshold for the persisted equity baseline. If a loaded
# baseline deviates from the broker's current equity by more than this
# fraction, we reject it and rebase to the broker's value. Tightened from
# 0.50 to 0.20 after round-2 risk review: drift above 20% in practice means
# either a tampered file, a cross-account restore, or a market move large
# enough that the prior run's daily-loss cap should already have tripped —
# in all three cases, rebasing to the broker's view is the correct response.
_EQUITY_PERSISTENCE_MAX_DRIFT_PCT = Decimal("0.20")


@dataclass
class SessionConfig:
    symbols: list[str]
    poll_interval_seconds: int = 60
    lookback_minutes: int = 60 * 24 * 3  # 3 calendar days back
    max_iterations: int | None = None  # cap for tests; None = unlimited
    # Flatten positions on a CLEAN exit (manual Ctrl-C / SIGTERM, market close,
    # max_iterations reached). Default False: ORB emits its own flat signals
    # before close, and a manual stop is usually an operator intervention
    # rather than a "close everything" instruction. Positions roll across bot
    # restarts; the broker-side bracket children keep guarding them.
    flatten_on_exit: bool = False
    # Flatten on a HALT exit (kill-switch trip, broker auth/validation error,
    # consecutive broker failures). Default True: a halt means something is
    # wrong; closing positions while the broker is still reachable is the safe
    # reflex. Override only with deliberate operator opt-out.
    flatten_on_halt: bool = True
    max_consecutive_failures: int = 5
    # Override to redirect session_start_equity persistence (e.g. in tests).
    session_state_dir: Path | None = None
    # Override for the directory holding UNRECONCILED_* sentinel files.
    unreconciled_dir: Path | None = None
    # Override for the directory holding CARRIED_* overnight-position records.
    carried_dir: Path | None = None
    # If True, a prior run's unreconciled-positions sentinel does NOT block
    # startup. Operator opt-in only; the default refusal is the safe choice.
    force_ignore_unreconciled: bool = False
    # Overnight-holding feature gate (default OFF = current behavior). When True:
    #   1. entries are submitted GTC so the broker-side stop/take bracket
    #      survives the close and protects a carried position;
    #   2. on a clean exit that leaves positions open, a CARRIED_<run_id>.json
    #      record is written;
    #   3. on startup, broker positions that strictly match a fresh carried
    #      record are ADOPTED (no force_ignore_unreconciled needed) instead of
    #      refused. Anything that does not match still hits the orphan refusal.
    # NOTE: this flag is SESSION-WIDE, not per-strategy. With it on and
    # flatten_on_exit=False, ANY strategy's open position (including the
    # intraday orb/pullback bots) can be carried overnight on a clean exit, not
    # just insider. Enable only after confirming GTC bracket acceptance and
    # overnight survival on live Alpaca paper.
    protect_overnight: bool = False
    # Reject signals whose bar timestamp is older than this many minutes vs.
    # the current wall clock. Guards against a late-launched session stuffing
    # the book on hours-old opening-range breakouts (observed 2026-05-14).
    # Set to None to disable (used by tests that replay historical bars).
    max_signal_age_minutes: int | None = 5
    # Halt after this many consecutive transient BrokerError absorbed inside
    # the mid-sleep daily-loss check. Catches a flapping connection that
    # recovers each top-of-loop and would otherwise mask the cap indefinitely.
    # At 3 chunks × 5s × 3 failures ≈ 45s of blindness before halt.
    max_consecutive_midsleep_failures: int = 3
    # Maximum NEW entry signals to act on per iteration across all symbols.
    # When set, the cross-symbol picker ranks candidates by OR/ATR ratio and
    # keeps the top-N. None disables the picker (every fresh signal fires,
    # subject only to the risk-cap on max_position_count). For a wide
    # universe, set this to RiskParams.max_position_count so the bot picks
    # the strongest setups rather than acting on whichever fires first.
    max_concurrent_entries: int | None = None
    # Pool-entry floor for the picker — candidates below this OR/ATR ratio
    # are excluded before ranking. Defends a wide-universe deploy from
    # trading the "best of a bad lot" on slow days where all candidates
    # marginally clear the strategy filter (default 0.5) but none are
    # convincingly above noise. None disables the floor.
    picker_min_ratio: float | None = None
    # Optional run id; if None, run_session generates one in UTC ISO-basic form.
    run_id: str | None = None
    # Optional in-session strategy-data refresh. ``refresh_hook`` is a
    # no-argument callable invoked every ``refresh_interval_minutes`` of
    # wall-clock time, used today by the insider strategy to pull new Form 4
    # filings from EDGAR and rebuild its in-memory signal index. The hook
    # runs synchronously in the session's main loop (no thread), so it
    # MUST return well inside ``poll_interval_seconds`` — a hook that
    # blocks longer than the poll interval delays the next stop/take check
    # and bar fetch by exactly that amount. Recommended upper bound:
    # ~30s for the 60s default poll. Any exception raised by the hook is
    # caught, logged, and does not halt the session; ``last_refresh_at`` is
    # still advanced so a failing hook does not spin-retry every iteration.
    refresh_interval_minutes: int | None = None
    refresh_hook: Callable[[], None] | None = None
    # Per-strategy trailing-stop policy: strategy name → enabled. The session
    # reads this at entry time using the `strategy` column on the signal row
    # and stamps the trail state into open_entries when enabled. Missing keys
    # default to disabled. Defaults empty (no trailing); the runner builds the
    # dict from each strategy's enable_trailing_stop config field.
    trailing_stop_policy: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Fail fast on a misconfigured hook. Without this, a non-callable
        # would only blow up inside the loop's try/except where it would be
        # logged as "refresh_hook failed" every interval forever.
        if self.refresh_hook is not None and not callable(self.refresh_hook):
            raise TypeError(
                "SessionConfig.refresh_hook must be callable or None, "
                f"got {type(self.refresh_hook).__name__}"
            )
        if (
            self.refresh_interval_minutes is not None
            and self.refresh_interval_minutes <= 0
        ):
            raise ValueError(
                "SessionConfig.refresh_interval_minutes must be a positive "
                f"int or None (got {self.refresh_interval_minutes})"
            )


@dataclass(frozen=True)
class FlattenResult:
    """Outcome of `_flatten_all`. Reported in audit logs and on SessionState.

    A non-empty `positions_failed` means the bot exited with open positions
    the broker refused to close — `run_session` writes a sentinel file in
    that case so the next start refuses to proceed until reconciled.
    """

    cancel_ok: bool
    positions_closed: tuple[str, ...]
    positions_failed: tuple[str, ...]


@dataclass
class SessionState:
    """Mutable per-session counters and trackers.

    `realized_pnl_today` is computed as `current_equity - session_start_equity`,
    which is total daily P&L (realized + unrealized). This is conservative for
    the daily-loss kill switch: unrealized losses trip the cap too, which is
    the safer behavior for a paper-trading sandbox.
    """

    processed_signal_ts: dict[str, set[pd.Timestamp]] = field(default_factory=dict)
    cumulative_notional_today: Decimal = Decimal("0")
    realized_pnl_today: Decimal = Decimal("0")
    session_start_equity: Decimal | None = None
    iterations: int = 0
    consecutive_failures: int = 0
    # Counter for transient BrokerError absorbed inside the sleep helper's
    # daily-loss check. A flapping connection that recovers each top-of-loop
    # would otherwise mask mid-sleep blindness indefinitely. Resets on any
    # successful mid-sleep check.
    consecutive_midsleep_failures: int = 0
    stop_requested: bool = False
    halt_reason: str | None = None
    flatten_result: FlattenResult | None = None
    run_id: str | None = None
    # Completed round-trip trades for the session, used by the end-of-run
    # summary table. Each entry submission writes into `open_entries`; the
    # corresponding exit (flat signal or _flatten_all close) pairs and pops.
    trades: list = field(default_factory=list)
    open_entries: dict = field(default_factory=dict)
    # Wall-clock timestamp of the last successful refresh_hook invocation.
    # None until the first refresh fires (or always, if refresh is disabled).
    last_refresh_at: datetime | None = None
    # Copy of SessionConfig.trailing_stop_policy stashed at session start so
    # the helpers (`_record_entry`, `_check_stops_and_takes`) can look up
    # per-strategy trail enablement without threading SessionConfig through
    # every call site.
    trailing_stop_policy: Mapping[str, bool] = field(default_factory=dict)


def _install_signal_handlers(state: SessionState) -> None:
    def handler(signum, _frame):
        logger.warning("received signal %s — requesting stop", signum)
        state.stop_requested = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _resolve_dir(override: Path | None, default: Path) -> Path:
    return Path(override) if override is not None else default


def _session_state_path(config: SessionConfig, now_utc: datetime) -> Path:
    """Resolve the on-disk path for the current trading day's state snapshot.

    The filename is keyed on the ET date so a single calendar trading session
    maps to one file even if the bot is restarted multiple times that day.
    """
    et_date = now_utc.astimezone(ZoneInfo("America/New_York")).date()
    root = _resolve_dir(config.session_state_dir, _DEFAULT_SESSION_STATE_DIR)
    return root / f"{et_date.isoformat()}.json"


def _load_session_start_equity(path: Path) -> Decimal | None:
    """Read the persisted equity baseline if present; None on any failure."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Decimal(str(data["session_start_equity"]))
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
        logger.warning(
            "failed to load session_start_equity from %s: %s", path, exc
        )
        return None


def _save_session_start_equity(path: Path, equity: Decimal) -> None:
    """Persist the equity baseline atomically.

    Write to `<path>.tmp` and `os.replace` into place — a crash mid-write
    leaves either the previous file or the new one, never a truncated one.
    Failures are logged but never raised: persistence is an optimization
    over the first-poll fallback, not a correctness requirement.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"session_start_equity": str(equity)}))
        os.replace(tmp, path)
    except OSError:
        logger.exception("failed to persist session_start_equity to %s", path)
        # Best-effort cleanup of an orphan tempfile.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _baseline_drift_ratio(loaded: Decimal, current: Decimal) -> Decimal:
    """Relative drift |loaded - current| / current. Infinite if current <= 0."""
    if current <= 0:
        return Decimal("Infinity")
    return abs(loaded - current) / current


def _validate_persisted_baseline(
    loaded: Decimal, current_equity: Decimal
) -> bool:
    """Reject a persisted baseline that deviates from broker equity by more
    than `_EQUITY_PERSISTENCE_MAX_DRIFT_PCT`.

    Catches: tampered files, copied files from a different account, day-old
    files persisting under today's filename via a botched backup restore.
    A loaded value within the threshold is treated as authoritative (it
    preserves the intra-day daily-loss baseline across a crash-restart).
    """
    return (
        _baseline_drift_ratio(loaded, current_equity)
        <= _EQUITY_PERSISTENCE_MAX_DRIFT_PCT
    )


def _check_daily_loss_mid_sleep(
    broker: BrokerClient,
    risk_params: RiskParams,
    sess: SessionState,
    *,
    max_consecutive_midsleep_failures: int,
) -> str | None:
    """Refresh equity and run the daily-loss cap during the sleep window.

    A transient broker outage here is normally skipped — the top-of-loop on
    the next iteration will retry. But a flapping connection that recovers
    each top-of-loop would mask the daily-loss cap indefinitely, so the
    swallowed failures are counted and the loop halts after N consecutive.
    Returns the halt reason if the cap trips OR the counter exceeds the
    threshold, else None.
    """
    if sess.session_start_equity is None:
        return None
    try:
        account = broker.get_account()
    except (BrokerAuthError, BrokerValidationError):
        # Halt-class errors must surface — let the top-of-loop handler escalate
        # on the next iteration. Do NOT swallow as a transient outage would be.
        raise
    except BrokerError:
        sess.consecutive_midsleep_failures += 1
        logger.exception(
            "mid-sleep get_account failed; skipping daily-loss check (%d/%d)",
            sess.consecutive_midsleep_failures,
            max_consecutive_midsleep_failures,
        )
        if sess.consecutive_midsleep_failures >= max_consecutive_midsleep_failures:
            return (
                f"{sess.consecutive_midsleep_failures} consecutive mid-sleep "
                f"broker failures — daily-loss cap blind"
            )
        return None
    sess.consecutive_midsleep_failures = 0  # Reset on success.
    sess.realized_pnl_today = account.equity - sess.session_start_equity
    state = AccountState(
        equity=account.equity,
        cash=account.cash,
        realized_pnl_today=sess.realized_pnl_today,
        cumulative_notional_today=sess.cumulative_notional_today,
    )
    ok, reason = check_daily_loss(state, risk_params)
    return reason if not ok else None


def _sleep_with_safety_checks(
    *,
    total_seconds: int,
    sleep_fn,
    broker: BrokerClient,
    params: RiskParams,
    state: SessionState,
    max_consecutive_midsleep_failures: int,
    chunk_seconds: int = _KILL_CHECK_CHUNK_SECONDS,
    daily_loss_every_n: int = _DAILY_LOSS_CHECK_EVERY_N_CHUNKS,
) -> str | None:
    """Sleep up to `total_seconds`, polling safety checks between chunks.

    Cadence:
      • kill-file + kill-env  every chunk (~5s)
      • daily-loss cap        every Nth chunk (~15s)

    Returns a halt-reason string if any check trips, else None. Always
    invokes `sleep_fn` at least once so tests that hook the callback to
    mutate broker state still observe it.
    """
    if total_seconds <= 0:
        sleep_fn(0)
        return None
    remaining = total_seconds
    chunks_done = 0
    while remaining > 0:
        chunk = min(chunk_seconds, remaining)
        sleep_fn(chunk)
        remaining -= chunk
        chunks_done += 1
        if state.stop_requested:
            return None
        ok, reason = check_kill_file(params)
        if not ok:
            return reason
        ok, reason = check_kill_env(params)
        if not ok:
            return reason
        if chunks_done % daily_loss_every_n == 0:
            reason = _check_daily_loss_mid_sleep(
                broker,
                params,
                state,
                max_consecutive_midsleep_failures=max_consecutive_midsleep_failures,
            )
            if reason is not None:
                return reason
    return None


def _existing_unreconciled(config: SessionConfig) -> list[Path]:
    """List sentinel files in the unreconciled dir (sorted by name)."""
    root = _resolve_dir(config.unreconciled_dir, _DEFAULT_UNRECONCILED_DIR)
    if not root.exists():
        return []
    return sorted(root.glob("UNRECONCILED_*.json"))


def _write_unreconciled_sentinel(
    config: SessionConfig,
    run_id: str,
    failed_symbols: tuple[str, ...],
    now_utc: datetime,
) -> Path:
    """Write a sentinel file recording the failed-flatten symbols.

    The presence of any sentinel blocks the next `run_session` start unless
    `force_ignore_unreconciled` is set. The operator is expected to inspect,
    reconcile manually with the broker, and delete the sentinel.

    Never overwrites: if a same-second `run_id` collision would clash with
    an existing sentinel, an incrementing `_<n>` suffix is appended so the
    earlier record is preserved.
    """
    root = _resolve_dir(config.unreconciled_dir, _DEFAULT_UNRECONCILED_DIR)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"UNRECONCILED_{run_id}.json"
    n = 1
    while path.exists():
        path = root / f"UNRECONCILED_{run_id}_{n}.json"
        n += 1
    payload = {
        "run_id": run_id,
        "failed_symbols": list(failed_symbols),
        "created_at_utc": now_utc.astimezone(UTC).isoformat(),
    }
    # Atomic write: a crash mid-write leaves either no sentinel or the full
    # one, never a truncated payload. Symmetric with _save_session_start_equity.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except OSError:
        logger.exception("failed to write unreconciled sentinel to %s", path)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    return path


def _carried_positions_path(config: SessionConfig, run_id: str) -> Path:
    """Path for this run's carried-position record (never overwrites an older one)."""
    root = _resolve_dir(config.carried_dir, _DEFAULT_CARRIED_DIR)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"CARRIED_{run_id}.json"
    n = 1
    while path.exists():
        path = root / f"CARRIED_{run_id}_{n}.json"
        n += 1
    return path


def _write_carried_positions(
    config: SessionConfig,
    sess: SessionState,
    broker_positions: dict,
    now_utc: datetime,
) -> Path | None:
    """Record positions intentionally left open on a clean exit so the next
    start can ADOPT them (Phase 3) instead of hitting the orphan refusal.

    Qty is taken from the broker (authority); the richer entry metadata
    (stop/take/strategy/trail) comes from ``sess.open_entries`` when present.
    Stamped with the ET trading date for the startup freshness check.
    """
    if not broker_positions:
        return None
    et_date = now_utc.astimezone(ZoneInfo("America/New_York")).date()
    records = []
    for symbol, bpos in broker_positions.items():
        entry = sess.open_entries.get(symbol, {})
        side = entry.get("side") or ("long" if bpos.qty > 0 else "short")
        entry_price = entry.get("entry_price", bpos.avg_entry_price)
        records.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": str(abs(bpos.qty)),
                "entry_price": str(entry_price),
                "entry_time": (
                    entry["entry_time"].isoformat()
                    if entry.get("entry_time") is not None
                    else pd.Timestamp(now_utc).isoformat()
                ),
                "stop_price": str(entry["stop_price"]) if entry.get("stop_price") is not None else None,
                "take_price": str(entry["take_price"]) if entry.get("take_price") is not None else None,
                "strategy": entry.get("strategy", ""),
                "trail_enabled": bool(entry.get("trail_enabled", False)),
                "trail_offset": str(entry.get("trail_offset", "0")),
                "trail_extreme": str(entry.get("trail_extreme", entry_price)),
            }
        )
    payload = {
        "run_id": sess.run_id,
        "created_at_utc": now_utc.astimezone(UTC).isoformat(),
        "et_date": et_date.isoformat(),
        "positions": records,
    }
    path = _carried_positions_path(config, sess.run_id or "unknown")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except OSError:
        logger.exception("failed to write carried-position record to %s", path)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None
    logger.info(
        "wrote carried-position record",
        extra={"path": str(path), "symbols": [r["symbol"] for r in records]},
    )
    return path


def _load_carried_record(config: SessionConfig, now_utc: datetime) -> dict | None:
    """Return the newest fresh, well-formed carried-position record, or None.

    None means "no usable record" → the caller falls back to the orphan
    refusal. A parse failure or a stale (older than `_CARRIED_MAX_AGE_DAYS`)
    record is treated as no record — never adopted.
    """
    root = _resolve_dir(config.carried_dir, _DEFAULT_CARRIED_DIR)
    if not root.exists():
        return None
    now_et = now_utc.astimezone(ZoneInfo("America/New_York")).date()
    # Newest first: run_id embeds a sortable UTC basic-form timestamp.
    for path in sorted(root.glob("CARRIED_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text())
            rec_date = date.fromisoformat(data["et_date"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            logger.warning("ignoring unreadable carried record %s: %s", path, exc)
            continue
        age_days = (now_et - rec_date).days
        if age_days < 0 or age_days > _CARRIED_MAX_AGE_DAYS:
            logger.warning(
                "ignoring stale carried record %s (et_date=%s, age=%dd)",
                path, rec_date, age_days,
            )
            continue
        if not isinstance(data.get("positions"), list):
            logger.warning("ignoring carried record %s: malformed positions", path)
            continue
        return data
    return None


def _adopt_carried_positions(record: dict, broker_positions: dict) -> dict | None:
    """Strictly match a carried record against live broker positions.

    Returns the open-entries dict to adopt on success, or None to REFUSE.
    The broker is the source of truth for quantity. Refuses if any broker
    position is absent from the record, if the broker holds MORE than recorded,
    or on a side / avg-price mismatch. A recorded position absent at the broker
    exited overnight — dropped from adoption, not a refusal.
    """
    rec_by_sym = {p["symbol"]: p for p in record["positions"]}
    adopted: dict = {}
    for symbol, bpos in broker_positions.items():
        rec = rec_by_sym.get(symbol)
        if rec is None:
            logger.error("carried-adopt refuse: broker position %s not in record", symbol)
            return None
        try:
            rec_qty = Decimal(str(rec["qty"]))
            rec_price = Decimal(str(rec["entry_price"]))
        except (KeyError, ValueError):
            logger.error("carried-adopt refuse: malformed record for %s", symbol)
            return None
        broker_qty_abs = abs(bpos.qty)
        if broker_qty_abs > rec_qty:
            logger.error(
                "carried-adopt refuse: broker holds MORE than recorded for %s "
                "(broker=%s recorded=%s)", symbol, broker_qty_abs, rec_qty,
            )
            return None
        broker_side = "long" if bpos.qty > 0 else "short"
        if broker_side != rec.get("side"):
            logger.error(
                "carried-adopt refuse: side mismatch for %s (broker=%s recorded=%s)",
                symbol, broker_side, rec.get("side"),
            )
            return None
        if rec_price > 0:
            drift = abs(rec_price - bpos.avg_entry_price) / rec_price
            if drift > _CARRIED_PRICE_TOLERANCE_PCT:
                logger.error(
                    "carried-adopt refuse: avg-price mismatch for %s "
                    "(broker=%s recorded=%s)", symbol, bpos.avg_entry_price, rec_price,
                )
                return None
        try:
            adopted[symbol] = {
                "side": rec["side"],
                "qty": broker_qty_abs,  # broker is authority (may be < recorded)
                "entry_price": rec_price,
                "entry_time": pd.Timestamp(rec["entry_time"]),
                "stop_price": Decimal(str(rec["stop_price"])) if rec.get("stop_price") else None,
                "take_price": Decimal(str(rec["take_price"])) if rec.get("take_price") else None,
                "strategy": rec.get("strategy", ""),
                "trail_enabled": bool(rec.get("trail_enabled", False)),
                "trail_offset": Decimal(str(rec.get("trail_offset", "0"))),
                "trail_extreme": Decimal(str(rec.get("trail_extreme", rec_price))),
            }
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            # A corrupt field in an otherwise-matching record must refuse
            # cleanly, not crash run_session with an uncaught exception.
            logger.error("carried-adopt refuse: corrupt record for %s: %s", symbol, exc)
            return None
    absent = set(rec_by_sym) - set(broker_positions)
    if absent:
        logger.info("carried positions exited overnight (not adopted): %s", sorted(absent))
    return adopted


def _clear_carried_records(config: SessionConfig) -> None:
    """Delete all carried-position records (best-effort). Called once a record
    has been consumed by adoption so it cannot be matched again."""
    root = _resolve_dir(config.carried_dir, _DEFAULT_CARRIED_DIR)
    if not root.exists():
        return
    for path in root.glob("CARRIED_*.json"):
        try:
            path.unlink()
        except OSError:
            logger.exception("failed to remove consumed carried record %s", path)


def _normalize_status(status: object) -> str:
    """Lowercase a broker status and strip any enum prefix.

    The Alpaca adapter stores `str(OrderStatus.PARTIALLY_FILLED)` which renders
    as `"OrderStatus.PARTIALLY_FILLED"`, while the test fakes use plain
    `"filled"`/`"accepted"`. Normalize both to the bare token (e.g.
    `"partially_filled"`) so status comparisons work against either source.
    """
    s = str(status).lower()
    return s.rsplit(".", 1)[-1] if "." in s else s


def _is_fully_filled(resp: BrokerOrderResponse, ordered_qty: Decimal) -> bool:
    """True once the whole order is filled.

    Two independent signals, either sufficient: a `filled` status, or
    `filled_qty` reaching the ordered quantity. Using filled_qty makes this
    robust to the partial-fill case where `filled_avg_price` becomes non-null
    on the FIRST partial — which previously short-circuited the poll and let
    the loop book the ordered qty as if the whole order had filled.
    """
    if _normalize_status(resp.status) == "filled":
        return True
    return (
        resp.filled_qty is not None
        and ordered_qty is not None
        and resp.filled_qty >= ordered_qty
    )


def _await_fill(
    broker: BrokerClient,
    resp: BrokerOrderResponse,
    *,
    sleep_fn=time.sleep,
) -> BrokerOrderResponse:
    """Poll the broker until the order is fully filled, goes terminal, or
    attempts run out.

    Market orders return `accepted` synchronously; fills arrive async and may
    arrive in pieces. We must NOT stop at the first partial fill: the caller
    books `resp.filled_qty`, so returning a half-filled response would record
    only part of the position (or, under the old `filled_avg_price is not
    None` short-circuit, mislabel a partial as complete). Returns the freshest
    response seen; on a stuck partial it returns that partial after the attempt
    budget, and the caller records the actually-filled quantity.
    """
    ordered_qty = resp.qty
    if _is_fully_filled(resp, ordered_qty):
        return resp
    latest = resp
    for _ in range(_FILL_POLL_MAX_ATTEMPTS):
        sleep_fn(_FILL_POLL_INTERVAL_SECONDS)
        try:
            latest = broker.get_order(resp.broker_order_id)
        except (BrokerAuthError, BrokerValidationError):
            # Halt-class — surface so the top-of-loop handler escalates.
            raise
        except BrokerError:
            logger.exception(
                "get_order failed during fill poll for %s; retrying",
                resp.broker_order_id,
            )
            continue
        if _is_fully_filled(latest, ordered_qty):
            logger.info(
                "order filled",
                extra={
                    "broker_order_id": latest.broker_order_id,
                    "status": latest.status,
                    "symbol": latest.symbol,
                    "filled_qty": str(latest.filled_qty),
                    "filled_avg_price": str(latest.filled_avg_price),
                },
            )
            return latest
        if _normalize_status(latest.status) in _TERMINAL_ORDER_STATUSES:
            # rejected / canceled / expired / done_for_day — it will not fill
            # any further; return whatever (possibly partial) fill we have.
            return latest
    return latest


def _record_entry(
    sess: SessionState,
    symbol: str,
    side: str,
    order: ProposedOrder,
    resp: BrokerOrderResponse,
    *,
    strategy: str = "",
) -> None:
    """Stash an open-entry record and print a live ENTRY line to stdout.

    Pairs with `_record_exit` when the strategy emits a flat signal or
    `_flatten_all` closes the position at session end. Symbols that the
    bot did not open in this session are not tracked. ``strategy`` is the
    producing bot's name ("orb"|"pullback"|"insider"|"") and rides through
    to the eventual Trade record + log lines so trades can be attributed.
    """
    entry_price = (
        resp.filled_avg_price
        if resp.filled_avg_price is not None
        else order.limit_price
    )
    if entry_price is None:
        logger.warning(
            "entry fill price unavailable after poll; recording as 0",
            extra={
                "symbol": symbol,
                "broker_order_id": resp.broker_order_id,
                "status": resp.status,
            },
        )
        entry_price = Decimal("0")
    entry_time = pd.Timestamp(resp.submitted_at)
    # Book what actually filled, not what we asked for. A partially-filled
    # market order reports filled_qty < order.qty; recording the ordered qty
    # desyncs the bot's book from the broker and produces the later
    # "close_position no-op: ... has no position" seen on 2026-05-27. Fall
    # back to the ordered qty only when the broker reported no fill quantity
    # at all (defensive; a real fill always carries a quantity).
    fill_qty = (
        resp.filled_qty
        if resp.filled_qty is not None and resp.filled_qty > 0
        else order.qty
    )
    # Stash trailing-stop state if the producing strategy opts in AND a stop
    # was set at entry. trail_offset is a fixed dollar distance computed once
    # from entry_price and the initial stop; the ratchet engine in
    # `_check_stops_and_takes` uses it to recompute the stop each poll while
    # never letting it move against the position.
    trail_enabled = bool(
        sess.trailing_stop_policy.get(strategy, False)
        and order.stop_price is not None
        and entry_price > 0
    )
    if trail_enabled:
        offset = trail_offset(entry_price, order.stop_price, side)
        trail_extreme = entry_price
    else:
        offset = Decimal("0")
        trail_extreme = entry_price
    sess.open_entries[symbol] = {
        "side": side,
        "qty": fill_qty,
        "entry_price": entry_price,
        "entry_time": entry_time,
        "stop_price": order.stop_price,
        "take_price": order.take_price,
        "strategy": strategy,
        "trail_enabled": trail_enabled,
        "trail_offset": offset,
        "trail_extreme": trail_extreme,
    }
    line = render_trade_event(
        event="ENTRY",
        symbol=symbol,
        side=side,
        qty=fill_qty,
        price=entry_price,
        timestamp=entry_time,
        stop_price=order.stop_price,
        take_price=order.take_price,
        strategy=strategy,
    )
    print(line, flush=True)
    logger.info(
        "trade entry",
        extra={
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "qty": str(fill_qty),
            "entry_price": str(entry_price),
            "stop_price": str(order.stop_price) if order.stop_price else None,
            "take_price": str(order.take_price) if order.take_price else None,
        },
    )


def _record_exit(
    sess: SessionState,
    symbol: str,
    resp: BrokerOrderResponse | None,
    exit_reason: str,
) -> None:
    """Pair an exit with the matching open entry; produce a Trade record."""
    entry = sess.open_entries.pop(symbol, None)
    if entry is None or resp is None:
        return
    if resp.filled_avg_price is None:
        logger.warning(
            "exit fill price unavailable after poll; pnl will be 0",
            extra={
                "symbol": symbol,
                "broker_order_id": resp.broker_order_id,
                "status": resp.status,
            },
        )
    exit_price = (
        resp.filled_avg_price
        if resp.filled_avg_price is not None
        else entry["entry_price"]
    )
    exit_time = pd.Timestamp(resp.submitted_at)
    qty = entry["qty"]
    side = entry["side"]
    strategy = entry.get("strategy", "")
    if side == "long":
        pnl = (exit_price - entry["entry_price"]) * qty
    else:
        pnl = (entry["entry_price"] - exit_price) * qty
    trade = Trade(
        symbol=symbol,
        side=side,
        entry_time=entry["entry_time"],
        exit_time=exit_time,
        entry_price=entry["entry_price"],
        exit_price=exit_price,
        qty=qty,
        pnl=pnl,
        exit_reason=exit_reason,
        strategy=strategy,
    )
    sess.trades.append(trade)
    line = render_trade_event(
        event="EXIT",
        symbol=symbol,
        side=side,
        qty=qty,
        price=exit_price,
        timestamp=exit_time,
        pnl=pnl,
        reason=exit_reason,
        strategy=strategy,
    )
    print(line, flush=True)
    logger.info(
        "trade exit",
        extra={
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "qty": str(qty),
            "exit_price": str(exit_price),
            "pnl": str(pnl),
            "exit_reason": exit_reason,
        },
    )


def _account_state(
    broker: BrokerClient, sess: SessionState
) -> AccountState:
    """Build an AccountState from the broker's view + per-session counters."""
    acct = broker.get_account()
    positions_raw = broker.get_positions()
    positions = {
        sym: Position(
            symbol=sym,
            qty=p.qty,
            avg_entry_price=p.avg_entry_price,
        )
        for sym, p in positions_raw.items()
    }
    return AccountState(
        equity=acct.equity,
        cash=acct.cash,
        realized_pnl_today=sess.realized_pnl_today,
        cumulative_notional_today=sess.cumulative_notional_today,
        open_positions=positions,
        open_order_count=broker.get_open_order_count(),
        connection_ok=True,
    )


def _build_order(
    *,
    symbol: str,
    side: OrderSide,
    qty: Decimal,
    stop_price: Decimal | None,
    take_price: Decimal | None,
    client_order_id: str,
    time_in_force: str = "day",
) -> ProposedOrder:
    return ProposedOrder(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        stop_price=stop_price,
        take_price=take_price,
        client_order_id=client_order_id,
        time_in_force=time_in_force,
    )


def _handle_entry_signal(
    *,
    sig: dict,
    broker: BrokerClient,
    risk_params: RiskParams,
    sess: SessionState,
    sleep_fn=time.sleep,
    protect_overnight: bool = False,
) -> BrokerOrderResponse | None:
    symbol = str(sig["symbol"])
    side_str = str(sig["side"])
    side = OrderSide.BUY if side_str == "long" else OrderSide.SELL

    current_price = broker.get_latest_trade_price(symbol)
    state = _account_state(broker, sess)

    if symbol in state.open_positions and state.open_positions[symbol].qty != 0:
        logger.info(
            "skipping entry: already in position",
            extra={"symbol": symbol, "side": side_str},
        )
        return None

    stop = (
        Decimal(str(sig["stop_price"]))
        if pd.notna(sig["stop_price"])
        else None
    )
    take = (
        Decimal(str(sig["take_price"]))
        if pd.notna(sig["take_price"])
        else None
    )

    qty = size_position(
        equity=state.equity,
        target_pct=Decimal(str(sig["target_size_pct"])),
        entry_price=current_price,
        stop_price=stop,
        params=risk_params,
    )
    if qty <= 0:
        logger.warning(
            "skipping entry: sizing returned 0",
            extra={"symbol": symbol, "equity": str(state.equity)},
        )
        return None

    client_id = f"orb-{symbol}-{sig['timestamp'].isoformat()}-{sess.run_id}"
    # GTC so the broker-side bracket survives the close when the operator has
    # opted into overnight holding; otherwise DAY (expires at close).
    order = _build_order(
        symbol=symbol,
        side=side,
        qty=qty,
        stop_price=stop,
        take_price=take,
        client_order_id=client_id,
        time_in_force="gtc" if protect_overnight else "day",
    )

    decision = validate_order(
        order,
        state,
        risk_params,
        market_is_open=True,
        current_price=current_price,
    )
    if not decision.approved or decision.order is None:
        logger.warning(
            "order rejected by risk",
            extra={"reason": decision.reason, "symbol": symbol},
        )
        return None

    resp = broker.submit_order(decision.order)
    resp = _await_fill(broker, resp, sleep_fn=sleep_fn)
    filled_qty = resp.filled_qty if resp.filled_qty is not None else Decimal("0")
    if filled_qty <= 0:
        # Nothing filled within the poll budget (or the order went terminal
        # unfilled). Do not record a phantom position or charge notional — the
        # bot holds nothing. The broker-side bracket, if any, guards a zero
        # position trivially.
        logger.warning(
            "entry not filled; no position recorded",
            extra={
                "symbol": symbol,
                "ordered_qty": str(order.qty),
                "status": resp.status,
                "broker_order_id": resp.broker_order_id,
            },
        )
        return resp
    # Count the actually-filled notional toward the daily cap, preferring the
    # fill price and falling back to the price we sized against. Previously
    # this added the full ordered qty at current_price BEFORE the fill was
    # known, overstating deployed notional on partial fills.
    fill_price = (
        resp.filled_avg_price if resp.filled_avg_price is not None else current_price
    )
    sess.cumulative_notional_today += abs(filled_qty) * fill_price
    raw_strategy = sig.get("strategy", "")
    strategy = "" if raw_strategy is None or (isinstance(raw_strategy, float) and pd.isna(raw_strategy)) else str(raw_strategy)
    _record_entry(sess, symbol, side_str, decision.order, resp, strategy=strategy)
    return resp


def _check_stops_and_takes(
    *,
    broker: BrokerClient,
    sess: SessionState,
    sleep_fn=time.sleep,
) -> None:
    """For every tracked open entry, close the position if the latest trade
    price has breached its stored stop or take level.

    Runs once per poll iteration as defense-in-depth alongside the broker-side
    bracket children attached at submit time. This bot-side check is the
    primary path while the session is running — it produces a clean Trade
    record with exit_reason="stop"/"take". Brackets are the safety net for
    when the bot is offline; if a bracket fires while we're down, the
    `open_entries` row is dropped at session end without a Trade.

    Stop wins over take when both are simultaneously breached (e.g. a wide
    intrabar move): preserving capital takes priority over locking in a gain.
    """
    for symbol, entry in list(sess.open_entries.items()):
        stop = entry.get("stop_price")
        take = entry.get("take_price")
        if stop is None and take is None:
            continue
        try:
            price = broker.get_latest_trade_price(symbol)
        except (BrokerAuthError, BrokerValidationError):
            raise
        except BrokerError:
            logger.exception(
                "stop/take check: get_latest_trade_price failed",
                extra={"symbol": symbol},
            )
            continue
        side = entry["side"]
        # Trailing-stop ratchet: update the per-position high/low water mark
        # using the latest trade price, recompute the trailing stop level,
        # and if it has moved in our favor, replace the broker-side stop leg
        # before re-checking the trigger. The broker bracket is the safety
        # net for intrabar moves the polling cadence misses.
        if (
            entry.get("trail_enabled")
            and stop is not None
            and entry.get("trail_offset") is not None
        ):
            extreme = entry["trail_extreme"]
            new_extreme = (
                max(extreme, price) if side == "long" else min(extreme, price)
            )
            if new_extreme != extreme:
                entry["trail_extreme"] = new_extreme
            new_stop = ratchet_stop(
                side=side,
                current_stop=stop,
                extreme_price=new_extreme,
                offset=entry["trail_offset"],
            )
            if new_stop != stop:
                try:
                    replaced = broker.replace_stop_price(symbol, new_stop)
                except (BrokerAuthError, BrokerValidationError):
                    raise
                except BrokerError:
                    logger.exception(
                        "trailing-stop replace failed; keeping prior stop",
                        extra={
                            "symbol": symbol,
                            "attempted_stop": str(new_stop),
                        },
                    )
                    replaced = False
                if replaced:
                    old_stop = stop
                    entry["stop_price"] = new_stop
                    stop = new_stop
                    trail_line = render_trade_event(
                        event="TRAIL",
                        symbol=symbol,
                        side=side,
                        qty=entry["qty"],
                        price=price,
                        timestamp=datetime.now(UTC),
                        stop_price=new_stop,
                        reason=f"from {old_stop}",
                        strategy=entry.get("strategy", ""),
                    )
                    print(trail_line, flush=True)
                    logger.info(
                        "trailing stop ratcheted",
                        extra={
                            "symbol": symbol,
                            "strategy": entry.get("strategy", ""),
                            "side": side,
                            "old_stop_price": str(old_stop),
                            "new_stop_price": str(new_stop),
                            "extreme_price": str(new_extreme),
                            "price": str(price),
                        },
                    )
        triggered: str | None = None
        if side == "long":
            if stop is not None and price <= stop:
                triggered = "stop"
            elif take is not None and price >= take:
                triggered = "take"
        else:
            if stop is not None and price >= stop:
                triggered = "stop"
            elif take is not None and price <= take:
                triggered = "take"
        if triggered is None:
            continue
        logger.info(
            "stop/take triggered",
            extra={
                "symbol": symbol,
                "reason": triggered,
                "price": str(price),
                "side": side,
                "stop_price": str(stop) if stop is not None else None,
                "take_price": str(take) if take is not None else None,
            },
        )
        try:
            canceled = broker.cancel_orders_for(symbol)
        except (BrokerAuthError, BrokerValidationError):
            raise
        except BrokerError:
            logger.exception(
                "cancel_orders_for failed; proceeding with close",
                extra={"symbol": symbol},
            )
            canceled = 0
        if canceled:
            logger.info(
                "canceled pending order(s) before stop/take close",
                extra={"symbol": symbol, "canceled": canceled},
            )
        resp = broker.close_position(symbol)
        if resp is not None:
            resp = _await_fill(broker, resp, sleep_fn=sleep_fn)
        _record_exit(sess, symbol, resp, exit_reason=triggered)


#Rename if new strategy is implemented, it will cause conflict
def _handle_flat_signal(
    *,
    sig: dict,
    broker: BrokerClient,
    sess: SessionState,
    sleep_fn=time.sleep,
) -> BrokerOrderResponse | None:
    symbol = str(sig["symbol"])
    state = _account_state(broker, sess)
    if symbol not in state.open_positions:
        return None
    # Cancel any pending orders for the symbol before closing. Guards against
    # Alpaca's wash-trade rejection (code 40310000) observed on 2026-05-14
    # where a flat signal fired while the entry was still PENDING_NEW and the
    # close_position market order was rejected as an "opposite side market".
    try:
        canceled = broker.cancel_orders_for(symbol)
    except (BrokerAuthError, BrokerValidationError):
        raise
    except BrokerError:
        logger.exception(
            "cancel_orders_for failed; proceeding with close",
            extra={"symbol": symbol},
        )
        canceled = 0
    if canceled:
        logger.info(
            "canceled pending order(s) before flat",
            extra={"symbol": symbol, "canceled": canceled},
        )
    logger.info("flat signal: closing position", extra={"symbol": symbol})
    resp = broker.close_position(symbol)
    if resp is not None:
        resp = _await_fill(broker, resp, sleep_fn=sleep_fn)
    _record_exit(sess, symbol, resp, exit_reason="time")
    return resp


def _select_entries(
    entry_candidates: dict[str, dict],
    *,
    max_concurrent_entries: int | None,
    min_ratio: float | None = None,
) -> dict[str, dict]:
    """Wrap the picker with the None-means-no-filter convention.

    Returns the candidate dict unchanged when max_concurrent_entries is None.
    Otherwise delegates to `rank_entry_signals` for top-N selection (with
    optional pool-entry ratio floor). Returns a new dict — does not mutate
    the input.
    """
    if max_concurrent_entries is None:
        return dict(entry_candidates)
    return rank_entry_signals(
        entry_candidates,
        max_picks=max_concurrent_entries,
        min_ratio=min_ratio,
    )


def _new_signals(
    signals: pd.DataFrame,
    symbol: str,
    sess: SessionState,
    *,
    now_utc: datetime,
    max_age_minutes: int | None,
) -> list[dict]:
    seen = sess.processed_signal_ts.setdefault(symbol, set())
    sym_signals = signals[signals["symbol"] == symbol]
    if sym_signals.empty:
        return []
    # Live semantics: act only on the most recent bar's signal. Older
    # signals (e.g. an opening-range breakout from earlier this morning)
    # are stale at current prices, and rapid open/close roundtrips trip
    # Alpaca's wash-trade protection. Only the latest per-symbol signal is
    # age-checked — strategies that emit multiple unprocessed bars in one
    # frame would surface only the newest.
    sym_signals = sym_signals.sort_values("timestamp")
    latest = sym_signals.iloc[-1]
    if latest["timestamp"] in seen:
        return []
    # Staleness guard applies to entries only. A stale FLAT means "you should
    # already be out" — skipping it would leave a position open until session
    # end, which is worse than acting on the old instruction. The dedup `seen`
    # set still gets the timestamp so a stale signal is not re-evaluated.
    seen.add(latest["timestamp"])
    side = str(latest["side"])
    if max_age_minutes is None or side == "flat":
        return [latest.to_dict()]
    sig_ts = pd.Timestamp(latest["timestamp"])
    if sig_ts.tzinfo is None:
        sig_ts = sig_ts.tz_localize(UTC)
    age = now_utc - sig_ts.to_pydatetime()
    if age > timedelta(minutes=max_age_minutes):
        logger.warning(
            "skipping stale signal",
            extra={
                "symbol": symbol,
                "side": side,
                "signal_ts": sig_ts.isoformat(),
                "age_minutes": round(age.total_seconds() / 60, 1),
                "max_age_minutes": max_age_minutes,
            },
        )
        return []
    return [latest.to_dict()]


def _fetch_bars_for_symbols(
    broker: BrokerClient,
    symbols: Iterable[str],
    lookback_minutes: int,
    *,
    now: datetime,
) -> dict[str, pd.DataFrame]:
    end = now - timedelta(minutes=1)
    start = end - timedelta(minutes=lookback_minutes)
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        out[symbol] = broker.get_recent_bars(symbol, start, end)
    return out


def _flatten_all(
    broker: BrokerClient,
    sess: SessionState | None = None,
    *,
    sleep_fn=time.sleep,
) -> FlattenResult:
    """Cancel all open orders, then close every open position.

    Catches `BrokerError` only — unexpected exceptions propagate so genuine
    bugs surface instead of being swallowed during shutdown. Returns an
    aggregate result for audit-log inspection.

    If `sess` is provided, each successful close pairs with a matching open
    entry (if any) and produces a `Trade` record in `sess.trades` with
    `exit_reason="session_end"`. Symbols not opened by this session are
    closed without producing a Trade — they were not ours to track.
    """
    logger.warning("flattening all positions")
    cancel_ok = True
    closed: list[str] = []
    failed: list[str] = []

    try:
        broker.cancel_all_orders()
    except BrokerError:
        logger.exception("cancel_all_orders failed during flatten")
        cancel_ok = False

    try:
        positions = broker.get_positions()
    except BrokerError:
        logger.exception("get_positions failed during flatten")
        return FlattenResult(
            cancel_ok=cancel_ok,
            positions_closed=tuple(closed),
            positions_failed=tuple(failed),
        )

    for symbol in positions:
        try:
            resp = broker.close_position(symbol)
            closed.append(symbol)
            if resp is not None:
                resp = _await_fill(broker, resp, sleep_fn=sleep_fn)
            if sess is not None:
                _record_exit(sess, symbol, resp, exit_reason="session_end")
        except BrokerError:
            logger.exception("close_position failed during flatten for %s", symbol)
            failed.append(symbol)

    return FlattenResult(
        cancel_ok=cancel_ok,
        positions_closed=tuple(closed),
        positions_failed=tuple(failed),
    )


def run_session(
    broker: BrokerClient,
    strategy: Strategy,
    config: SessionConfig,
    risk_params: RiskParams,
    *,
    sleep_fn=time.sleep,
    now_fn=lambda: datetime.now(UTC),
    install_signals: bool = True,
) -> SessionState:
    """Main run loop. Exits when the market closes, stop is requested, or
    `config.max_iterations` is reached.
    """
    if not broker.is_paper:
        raise RuntimeError(
            "run_session refuses a non-paper broker. Live trading is gated separately."
        )

    # Refuse to start if a prior run left UNRECONCILED_*.json sentinel(s).
    # The operator must inspect, reconcile manually, and remove the file —
    # or pass force_ignore_unreconciled=True to override.
    blockers = _existing_unreconciled(config)
    if blockers and not config.force_ignore_unreconciled:
        names = ", ".join(p.name for p in blockers)
        raise RuntimeError(
            f"Refusing to start: {len(blockers)} unreconciled flatten "
            f"failure(s) found ({names}) in {blockers[0].parent}. "
            "Reconcile manually with the broker, remove the file(s), or pass "
            "force_ignore_unreconciled=True to override."
        )
    if blockers and config.force_ignore_unreconciled:
        logger.error(
            "starting despite unreconciled flatten failures",
            extra={"blockers": [str(p) for p in blockers]},
        )

    # Reconcile-on-startup: a non-graceful prior exit (SIGKILL, OOM, power
    # loss) skips the flatten path, so positions remain at the broker with
    # no sentinel and no session record. Refuse to start in that case rather
    # than risk operating against an account state we did not author. Use
    # force_ignore_unreconciled=True for the legitimate "I have positions
    # outside the bot" case after manual review.
    try:
        existing_positions = broker.get_positions()
    except BrokerError:
        logger.exception(
            "broker.get_positions failed during startup reconcile; "
            "proceeding — main loop will retry"
        )
        existing_positions = {}
    # Adopted overnight positions, applied to sess.open_entries after creation.
    adopted_entries: dict = {}
    if existing_positions and not config.force_ignore_unreconciled:
        # Phase 3: when overnight holding is enabled, broker positions that
        # STRICTLY match a fresh carried-position record are adopted (the
        # intentional-carry case) rather than refused. A sentinel blocker above
        # always takes precedence — a carried record never overrides it. Any
        # position that does not match still hits the orphan refusal below.
        if config.protect_overnight:
            record = _load_carried_record(config, now_fn())
            if record is not None:
                adopted_entries = _adopt_carried_positions(record, existing_positions) or {}
        if not adopted_entries:
            symbols = ", ".join(sorted(existing_positions.keys()))
            logger.error(
                "refusing to start: orphan broker positions detected",
                extra={"positions": list(existing_positions.keys())},
            )
            raise RuntimeError(
                f"Refusing to start: {len(existing_positions)} open broker "
                f"position(s) ({symbols}) without a clean shutdown record. "
                "These may be orphans from a non-graceful prior exit (SIGKILL, "
                "crash, power loss) — or they may be legitimate positions from "
                "an intentional mid-day restart or out-of-band activity. "
                "Reconcile manually (flatten at broker or verify intent), then "
                "pass force_ignore_unreconciled=True to override."
            )
        logger.warning(
            "adopting carried overnight positions",
            extra={"positions": sorted(adopted_entries.keys())},
        )
        _clear_carried_records(config)
    if existing_positions and config.force_ignore_unreconciled:
        logger.error(
            "starting with pre-existing broker positions",
            extra={"positions": list(existing_positions.keys())},
        )

    sess = SessionState()
    sess.run_id = config.run_id or now_fn().strftime("%Y%m%dT%H%M%SZ")
    sess.trailing_stop_policy = dict(config.trailing_stop_policy)
    # Adopt carried overnight positions so stop/take management and trade
    # pairing continue for them (qty already reconciled to the broker above).
    if adopted_entries:
        sess.open_entries.update(adopted_entries)

    if install_signals:
        _install_signal_handlers(sess)

    # Resolve persistence path against the current ET trading day and try to
    # restore a prior baseline so a mid-day restart does not rebase the
    # daily-loss cap against an already-lower equity.
    state_path = _session_state_path(config, now_fn())
    persisted = _load_session_start_equity(state_path)
    needs_baseline_validation = False
    if persisted is not None:
        sess.session_start_equity = persisted
        needs_baseline_validation = True
        logger.info(
            "loaded session_start_equity from disk",
            extra={"equity": str(persisted), "path": str(state_path)},
        )

    logger.info(
        "session starting",
        extra={
            "symbols": config.symbols,
            "poll_interval_seconds": config.poll_interval_seconds,
            "paper": broker.is_paper,
            "session_state_path": str(state_path),
            "run_id": sess.run_id,
            "max_signal_age_minutes": config.max_signal_age_minutes,
        },
    )

    while not sess.stop_requested:
        if (
            config.max_iterations is not None
            and sess.iterations >= config.max_iterations
        ):
            logger.info("max_iterations reached, stopping")
            break
        sess.iterations += 1

        # State-free kill switches checked BEFORE any broker call this iteration.
        ok, reason = check_kill_file(risk_params)
        if not ok:
            sess.halt_reason = reason
            logger.warning("kill_file tripped, halting: %s", reason)
            break
        ok, reason = check_kill_env(risk_params)
        if not ok:
            sess.halt_reason = reason
            logger.warning("kill_env tripped, halting: %s", reason)
            break

        try:
            if not broker.is_market_open():
                logger.info("market closed, stopping")
                break

            # Periodic strategy-data refresh (e.g. pull new Form 4 filings
            # from EDGAR and rebuild the insider signal index). Runs in this
            # thread so it must return promptly; any exception is logged and
            # absorbed so a transient EDGAR hiccup never halts trading. The
            # refresh's data flows only into strategy.generate_signals on
            # subsequent iterations — it never touches the order, risk, or
            # position-tracking paths in this loop.
            if (
                config.refresh_interval_minutes is not None
                and config.refresh_hook is not None
            ):
                _now = now_fn()
                _last = sess.last_refresh_at
                _due = _last is None or (
                    (_now - _last).total_seconds()
                    >= config.refresh_interval_minutes * 60
                )
                if _due:
                    try:
                        config.refresh_hook()  # type: ignore[operator]
                        sess.last_refresh_at = _now
                    except Exception:
                        # Never let a refresh failure halt the session; the
                        # previous signal set remains in effect.
                        logger.exception("strategy refresh_hook failed")
                        sess.last_refresh_at = _now

            account = broker.get_account()
            if sess.session_start_equity is None:
                # First poll of a fresh session: seed baseline + persist.
                sess.session_start_equity = account.equity
                _save_session_start_equity(state_path, sess.session_start_equity)
                logger.info(
                    "session_start_equity set",
                    extra={
                        "equity": str(sess.session_start_equity),
                        "path": str(state_path),
                    },
                )
            elif needs_baseline_validation:
                # First poll after loading a persisted baseline — sanity-check
                # against the broker's current equity. Catches tampered or
                # cross-account files; rebases on failure.
                needs_baseline_validation = False
                if not _validate_persisted_baseline(
                    sess.session_start_equity, account.equity
                ):
                    logger.error(
                        "persisted session_start_equity rejected — rebasing to broker equity",
                        extra={
                            "loaded": str(sess.session_start_equity),
                            "current_equity": str(account.equity),
                            "max_drift_pct": str(_EQUITY_PERSISTENCE_MAX_DRIFT_PCT),
                        },
                    )
                    sess.session_start_equity = account.equity
                    _save_session_start_equity(
                        state_path, sess.session_start_equity
                    )
            sess.realized_pnl_today = account.equity - sess.session_start_equity

            # Defense-in-depth: close any position whose price has breached its
            # stored stop/take level before processing new signals. Runs before
            # bars are fetched so a stop-out frees its slot in the position cap
            # in time for this iteration's entry candidates.
            _check_stops_and_takes(
                broker=broker,
                sess=sess,
                sleep_fn=sleep_fn,
            )

            bars_by_symbol = _fetch_bars_for_symbols(
                broker,
                config.symbols,
                config.lookback_minutes,
                now=now_fn(),
            )

            # Two-pass: (1) collect fresh signals per symbol, (2) flats fire
            # unconditionally + entries go through the picker so a wide
            # universe doesn't deploy capital first-come-first-served.
            flat_signals: list[tuple[str, dict]] = []
            entry_candidates: dict[str, dict] = {}
            for symbol, bars in bars_by_symbol.items():
                if bars.empty:
                    continue
                signals = strategy.generate_signals(bars)
                if signals.empty:
                    continue
                for sig in _new_signals(
                    signals,
                    symbol,
                    sess,
                    now_utc=now_fn(),
                    max_age_minutes=config.max_signal_age_minutes,
                ):
                    if sig["side"] == "flat":
                        flat_signals.append((symbol, sig))
                    else:
                        entry_candidates[symbol] = sig

            # Flats always fire — closing exposure should never be gated.
            for symbol, sig in flat_signals:
                _handle_flat_signal(
                    sig=sig,
                    broker=broker,
                    sess=sess,
                    sleep_fn=sleep_fn,
                )

            # Entries: rank by quality, take top-N, fire in score order.
            chosen_entries = _select_entries(
                entry_candidates,
                max_concurrent_entries=config.max_concurrent_entries,
                min_ratio=config.picker_min_ratio,
            )
            if entry_candidates and len(chosen_entries) < len(entry_candidates):
                logger.info(
                    "picker selected entries",
                    extra={
                        "candidates": sorted(entry_candidates.keys()),
                        "chosen": sorted(chosen_entries.keys()),
                        "max_concurrent_entries": config.max_concurrent_entries,
                    },
                )
            for symbol, sig in chosen_entries.items():
                _handle_entry_signal(
                    sig=sig,
                    broker=broker,
                    risk_params=risk_params,
                    sess=sess,
                    sleep_fn=sleep_fn,
                    protect_overnight=config.protect_overnight,
                )

            sess.consecutive_failures = 0  # Reset on success.

        except (BrokerAuthError, BrokerValidationError) as exc:
            # Auth / validation errors cannot be resolved by retrying — the
            # API key is bad or the payload is malformed. Halt immediately
            # rather than burn the consecutive_failures budget.
            sess.halt_reason = f"broker {type(exc).__name__}: {exc}"
            logger.exception("halting on unrecoverable broker error")
            break
        except BrokerError:
            sess.consecutive_failures += 1
            logger.exception(
                "broker error this iteration; consecutive_failures=%d",
                sess.consecutive_failures,
            )
            if sess.consecutive_failures >= config.max_consecutive_failures:
                sess.halt_reason = (
                    f"{sess.consecutive_failures} consecutive broker failures"
                )
                logger.error("halting: %s", sess.halt_reason)
                break
        except Exception:
            sess.consecutive_failures += 1
            logger.exception(
                "unexpected error this iteration; consecutive_failures=%d",
                sess.consecutive_failures,
            )
            if sess.consecutive_failures >= config.max_consecutive_failures:
                sess.halt_reason = (
                    f"{sess.consecutive_failures} consecutive iteration failures"
                )
                logger.error("halting: %s", sess.halt_reason)
                break

        if sess.stop_requested:
            break
        try:
            sleep_reason = _sleep_with_safety_checks(
                total_seconds=config.poll_interval_seconds,
                sleep_fn=sleep_fn,
                broker=broker,
                params=risk_params,
                state=sess,
                max_consecutive_midsleep_failures=config.max_consecutive_midsleep_failures,
            )
        except (BrokerAuthError, BrokerValidationError) as exc:
            # An auth/validation error raised mid-sleep must NOT escape
            # run_session uncaught — that would bypass flatten_on_exit and
            # leave positions orphaned without a sentinel. Halt cleanly so
            # the post-loop flatten/sentinel block runs.
            sess.halt_reason = f"broker {type(exc).__name__} mid-sleep: {exc}"
            logger.exception("halting on unrecoverable mid-sleep broker error")
            break
        if sleep_reason is not None:
            sess.halt_reason = sleep_reason
            logger.warning("safety check tripped during sleep: %s", sleep_reason)
            break

    # Two-track flatten gate. halt_reason is set ONLY for fault-class exits
    # (kill switch trip, broker auth/validation error, consecutive failures);
    # market-close and SIGINT exits leave it None. The two paths consult
    # different config knobs so the operator can opt into closing positions
    # on clean exit without weakening the safety reflex on halt.
    should_flatten = (
        config.flatten_on_halt
        if sess.halt_reason is not None
        else config.flatten_on_exit
    )
    if should_flatten:
        sess.flatten_result = _flatten_all(broker, sess, sleep_fn=sleep_fn)
        if sess.flatten_result.positions_failed:
            # run_id is seeded unconditionally at session start (see SessionState
            # init above); assert here so a future regression that drops the
            # seeding fails loudly instead of writing a literal "unknown" sentinel.
            assert sess.run_id is not None, "run_id must be set by run_session"
            sentinel = _write_unreconciled_sentinel(
                config,
                sess.run_id,
                sess.flatten_result.positions_failed,
                now_fn(),
            )
            logger.error(
                "flatten completed with UNRECONCILED positions; sentinel written",
                extra={
                    "cancel_ok": sess.flatten_result.cancel_ok,
                    "positions_closed": list(sess.flatten_result.positions_closed),
                    "positions_failed": list(sess.flatten_result.positions_failed),
                    "sentinel_path": str(sentinel),
                },
            )
        else:
            logger.info(
                "flatten complete",
                extra={
                    "cancel_ok": sess.flatten_result.cancel_ok,
                    "positions_closed": list(sess.flatten_result.positions_closed),
                    "positions_failed": list(sess.flatten_result.positions_failed),
                },
            )
    elif config.protect_overnight:
        # Clean exit that intentionally leaves positions open: record them so
        # the next start adopts (not refuses) them. Their GTC brackets remain
        # the overnight protection. Skipped when flattening (nothing carried).
        try:
            remaining = broker.get_positions()
        except BrokerError:
            logger.exception("get_positions failed writing carried record")
            remaining = {}
        if remaining:
            _write_carried_positions(config, sess, remaining, now_fn())

    logger.info(
        "session ended",
        extra={"iterations": sess.iterations, "stopped": sess.stop_requested},
    )
    # End-of-session trade summary to stdout so the operator sees exactly
    # what the bot did. Audit log already has the per-event records.
    print()
    print(render_trade_table(sess.trades))
    print(render_summary_line(sess.trades))
    return sess
