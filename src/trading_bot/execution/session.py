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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable
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
from ..risk import size_position, validate_order
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
    flatten_on_exit: bool = True
    max_consecutive_failures: int = 5
    # Override to redirect session_start_equity persistence (e.g. in tests).
    session_state_dir: Path | None = None
    # Override for the directory holding UNRECONCILED_* sentinel files.
    unreconciled_dir: Path | None = None
    # If True, a prior run's unreconciled-positions sentinel does NOT block
    # startup. Operator opt-in only; the default refusal is the safe choice.
    force_ignore_unreconciled: bool = False
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


def _await_fill(
    broker: BrokerClient,
    resp: BrokerOrderResponse,
    *,
    sleep_fn=time.sleep,
) -> BrokerOrderResponse:
    """Poll the broker until the order has a fill price or attempts run out.

    Market orders return `accepted` synchronously; the fill arrives async.
    Returns the freshest response we got. May still have `filled_avg_price=None`
    on timeout — the caller logs a warning when it falls back.
    """
    if resp.filled_avg_price is not None:
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
        if latest.filled_avg_price is not None:
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
        if latest.status in _TERMINAL_ORDER_STATUSES:
            return latest
    return latest


def _record_entry(
    sess: SessionState,
    symbol: str,
    side: str,
    order: ProposedOrder,
    resp: BrokerOrderResponse,
) -> None:
    """Stash an open-entry record and print a live ENTRY line to stdout.

    Pairs with `_record_exit` when the strategy emits a flat signal or
    `_flatten_all` closes the position at session end. Symbols that the
    bot did not open in this session are not tracked.
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
    sess.open_entries[symbol] = {
        "side": side,
        "qty": order.qty,
        "entry_price": entry_price,
        "entry_time": entry_time,
        "stop_price": order.stop_price,
        "take_price": order.take_price,
    }
    line = render_trade_event(
        event="ENTRY",
        symbol=symbol,
        side=side,
        qty=order.qty,
        price=entry_price,
        timestamp=entry_time,
        stop_price=order.stop_price,
        take_price=order.take_price,
    )
    print(line, flush=True)
    logger.info(
        "trade entry",
        extra={
            "symbol": symbol,
            "side": side,
            "qty": str(order.qty),
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
    )
    print(line, flush=True)
    logger.info(
        "trade exit",
        extra={
            "symbol": symbol,
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
) -> ProposedOrder:
    return ProposedOrder(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        stop_price=stop_price,
        take_price=take_price,
        client_order_id=client_order_id,
    )


def _handle_entry_signal(
    *,
    sig: dict,
    broker: BrokerClient,
    risk_params: RiskParams,
    sess: SessionState,
    sleep_fn=time.sleep,
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
    order = _build_order(
        symbol=symbol,
        side=side,
        qty=qty,
        stop_price=stop,
        take_price=take,
        client_order_id=client_id,
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
    sess.cumulative_notional_today += abs(order.qty) * current_price
    resp = _await_fill(broker, resp, sleep_fn=sleep_fn)
    _record_entry(sess, symbol, side_str, decision.order, resp)
    return resp


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
    if existing_positions and not config.force_ignore_unreconciled:
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
    if existing_positions and config.force_ignore_unreconciled:
        logger.error(
            "starting with pre-existing broker positions",
            extra={"positions": list(existing_positions.keys())},
        )

    sess = SessionState()
    sess.run_id = config.run_id or now_fn().strftime("%Y%m%dT%H%M%SZ")

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

    if config.flatten_on_exit:
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
