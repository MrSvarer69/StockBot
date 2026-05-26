"""Single-symbol bar-by-bar backtest engine.

Honest-fill rules (enforced):
  - signal generated at bar t is filled at bar t+1 open (next-bar fill)
  - stops/takes are checked intrabar using the bar's high/low
  - costs (commission + slippage bps) charged on entry and exit, all to cash
  - end-of-session flat closes the position at the same bar's close

Bookkeeping symmetry:
  At entry we deduct the FULL round-trip commission (entry + exit) from cash.
  At exit we add the gross PnL to cash. This makes
  `initial_cash + sum(trade.pnl) == final_cash` hold exactly — see
  BacktestResult.reconcile().

Intrabar resolution:
  When a single bar's high/low touches both stop and take, the result is
  path-dependent and unknowable from OHLC alone. With `pessimistic_intrabar=True`
  (default) we always resolve to the stop, which is the conservative choice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from ..contracts import BacktestResult, RiskParams, Side, Trade
from ..risk import ratchet_stop, size_position


@dataclass(frozen=True)
class BacktestCosts:
    """Per-trade execution costs. Defaults err pessimistic."""

    commission_per_share: Decimal = Decimal("0.005")
    slippage_bps: Decimal = Decimal("1.0")  # 1 bp = 0.01%
    pessimistic_intrabar: bool = True


def _slip(price: float, side: Side, bps: Decimal) -> Decimal:
    bps_f = float(bps)
    mult = 1.0 + (bps_f / 10000.0) if side == "long" else 1.0 - (bps_f / 10000.0)
    return Decimal(str(price * mult))


def _gross_pnl(side: Side, entry: Decimal, exit_: Decimal, qty: Decimal) -> Decimal:
    if side == "long":
        return (exit_ - entry) * qty
    return (entry - exit_) * qty


def _resolve_intrabar(
    side: Side,
    bar_high: float,
    bar_low: float,
    stop: float | None,
    take: float | None,
    *,
    pessimistic: bool,
) -> tuple[Decimal | None, str | None]:
    """Pick (exit_price, exit_reason) for a single bar.

    Returns (None, None) if neither stop nor take is touched.
    """
    stop_hit = False
    take_hit = False
    if side == "long":
        stop_hit = stop is not None and bar_low <= stop
        take_hit = take is not None and bar_high >= take
    else:
        stop_hit = stop is not None and bar_high >= stop
        take_hit = take is not None and bar_low <= take

    if stop_hit and take_hit:
        if pessimistic:
            return Decimal(str(stop)), "stop"
        return Decimal(str(take)), "take"
    if stop_hit:
        return Decimal(str(stop)), "stop"
    if take_hit:
        return Decimal(str(take)), "take"
    return None, None


def run_backtest(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    *,
    initial_cash: Decimal = Decimal("100000"),
    risk_params: RiskParams | None = None,
    costs: BacktestCosts | None = None,
    symbol: str = "SPY",
    trailing_stop_policy: Mapping[str, bool] | None = None,
) -> BacktestResult:
    """Replay `signals` against `bars`, applying risk sizing and honest fills.

    `trailing_stop_policy` maps a strategy name (the value emitted in a signal
    row's ``strategy`` column — e.g. ``"orb"``, ``"pullback"``, ``"insider"``)
    to a bool indicating whether the trailing-stop ratchet should run for
    positions opened by that strategy. ``None`` (the default) disables the
    trail for every strategy, preserving the engine's original behavior.
    """
    risk_params = risk_params or RiskParams()
    costs = costs or BacktestCosts()
    policy: Mapping[str, bool] = trailing_stop_policy or {}

    if bars.empty:
        return BacktestResult(
            equity_curve=pd.Series(dtype="float64"),
            trades=[],
            metrics={"skipped_signals": 0},
            config={"initial_cash": str(initial_cash)},
        )

    cash = initial_cash
    open_side: Side | None = None
    open_qty = Decimal("0")
    open_entry = Decimal("0")
    open_entry_time = None
    open_stop: float | None = None
    open_take: float | None = None
    open_round_trip_fees = Decimal("0")
    open_strategy: str = ""  # Bot name from the entry signal; rides into the resulting Trade.
    open_trail_enabled: bool = False
    open_trail_offset: Decimal = Decimal("0")
    open_trail_extreme: Decimal = Decimal("0")

    equity_curve: dict[pd.Timestamp, float] = {}
    trades: list[Trade] = []
    skipped_signals = 0

    sig_by_time: dict[pd.Timestamp, dict] = {}
    if not signals.empty:
        for _, r in signals.iterrows():
            sig_by_time[r["timestamp"]] = r.to_dict()

    bar_index = bars.index
    pending_entry: dict | None = None

    for i, ts in enumerate(bar_index):
        row = bars.iloc[i]
        bar_open = float(row["open"])
        bar_high = float(row["high"])
        bar_low = float(row["low"])
        bar_close = float(row["close"])

        # 1) Apply any pending fill from a signal on the prior bar.
        if pending_entry is not None:
            side: Side = pending_entry["side"]
            entry_px = _slip(bar_open, side, costs.slippage_bps)
            qty = size_position(
                equity=cash,
                target_pct=Decimal(str(pending_entry["target_size_pct"])),
                entry_price=entry_px,
                stop_price=(
                    Decimal(str(pending_entry["stop_price"]))
                    if pd.notna(pending_entry["stop_price"])
                    else None
                ),
                params=risk_params,
            )
            if qty > 0:
                round_trip_fees = costs.commission_per_share * qty * Decimal("2")
                cash -= round_trip_fees
                open_side = side
                open_qty = qty
                open_entry = entry_px
                open_entry_time = ts
                open_stop = (
                    float(pending_entry["stop_price"])
                    if pd.notna(pending_entry["stop_price"])
                    else None
                )
                open_take = (
                    float(pending_entry["take_price"])
                    if pd.notna(pending_entry["take_price"])
                    else None
                )
                open_round_trip_fees = round_trip_fees
                raw_strategy = pending_entry.get("strategy", "")
                open_strategy = "" if raw_strategy is None or (isinstance(raw_strategy, float) and pd.isna(raw_strategy)) else str(raw_strategy)
                trail_enabled_for_strategy = bool(
                    policy.get(open_strategy, False)
                    and open_stop is not None
                )
                if trail_enabled_for_strategy:
                    open_trail_enabled = True
                    open_trail_offset = (
                        Decimal(str(entry_px)) - Decimal(str(open_stop))
                        if side == "long"
                        else Decimal(str(open_stop)) - Decimal(str(entry_px))
                    )
                    open_trail_extreme = Decimal(str(entry_px))
                else:
                    open_trail_enabled = False
                    open_trail_offset = Decimal("0")
                    open_trail_extreme = Decimal(str(entry_px))
            else:
                skipped_signals += 1
            pending_entry = None

        # 2) Intrabar stop/take check.
        if open_side is not None and open_entry_time is not None and open_entry_time != ts:
            # Ratchet the trailing stop using the bar's worst-case extreme
            # (high for longs, low for shorts) BEFORE resolving stops/takes.
            # Within a single bar OHLC alone cannot tell us the path, so we
            # update the extreme conservatively and let the (possibly
            # tightened) stop participate in the intrabar resolution.
            if open_trail_enabled and open_stop is not None:
                if open_side == "long":
                    open_trail_extreme = max(
                        open_trail_extreme, Decimal(str(bar_high))
                    )
                else:
                    open_trail_extreme = min(
                        open_trail_extreme, Decimal(str(bar_low))
                    )
                new_stop_dec = ratchet_stop(
                    side=open_side,
                    current_stop=Decimal(str(open_stop)),
                    extreme_price=open_trail_extreme,
                    offset=open_trail_offset,
                )
                # Engine carries stop as float for OHLC comparisons; keep types
                # consistent with the rest of the loop.
                open_stop = float(new_stop_dec)
            exit_px, exit_reason = _resolve_intrabar(
                open_side,
                bar_high,
                bar_low,
                open_stop,
                open_take,
                pessimistic=costs.pessimistic_intrabar,
            )
            if exit_px is not None and exit_reason is not None:
                gross = _gross_pnl(open_side, open_entry, exit_px, open_qty)
                cash += gross
                trades.append(
                    Trade(
                        symbol=symbol,
                        side=open_side,
                        entry_time=open_entry_time,
                        exit_time=ts,
                        entry_price=open_entry,
                        exit_price=exit_px,
                        qty=open_qty,
                        pnl=gross - open_round_trip_fees,
                        exit_reason=exit_reason,
                        strategy=open_strategy,
                    )
                )
                open_side = None
                open_qty = Decimal("0")
                open_entry = Decimal("0")
                open_entry_time = None
                open_stop = None
                open_take = None
                open_round_trip_fees = Decimal("0")
                open_strategy = ""
                open_trail_enabled = False
                open_trail_offset = Decimal("0")
                open_trail_extreme = Decimal("0")

        # 3) Process signal at this bar (effects pending for next bar's open).
        sig = sig_by_time.get(ts)
        if sig is not None:
            if sig["side"] == "flat":
                if open_side is not None:
                    exit_px = _slip(bar_close, open_side, costs.slippage_bps)
                    gross = _gross_pnl(open_side, open_entry, exit_px, open_qty)
                    cash += gross
                    trades.append(
                        Trade(
                            symbol=symbol,
                            side=open_side,
                            entry_time=open_entry_time,
                            exit_time=ts,
                            entry_price=open_entry,
                            exit_price=exit_px,
                            qty=open_qty,
                            pnl=gross - open_round_trip_fees,
                            exit_reason="time",
                            strategy=open_strategy,
                        )
                    )
                    open_side = None
                    open_qty = Decimal("0")
                    open_entry = Decimal("0")
                    open_entry_time = None
                    open_stop = None
                    open_take = None
                    open_round_trip_fees = Decimal("0")
                    open_strategy = ""
                    open_trail_enabled = False
                    open_trail_offset = Decimal("0")
                    open_trail_extreme = Decimal("0")
                # Drop any pending entry if a flat lands on the same bar.
                pending_entry = None
            else:
                if open_side is None and i + 1 < len(bar_index):
                    pending_entry = sig
                else:
                    # No room for a fill (last bar, or already in a position).
                    skipped_signals += 1

        # 4) Mark-to-market equity at bar close.
        if open_side is not None:
            mtm = Decimal(str(bar_close))
            unrealized = _gross_pnl(open_side, open_entry, mtm, open_qty)
            equity = cash + unrealized
        else:
            equity = cash
        equity_curve[ts] = float(equity)

    series = pd.Series(equity_curve, name="equity")
    series.index.name = "timestamp"
    metrics = _metrics(series, trades, initial_cash)
    metrics["skipped_signals"] = skipped_signals

    return BacktestResult(
        equity_curve=series,
        trades=trades,
        metrics=metrics,
        config={
            "initial_cash": str(initial_cash),
            "commission_per_share": str(costs.commission_per_share),
            "slippage_bps": str(costs.slippage_bps),
            "pessimistic_intrabar": costs.pessimistic_intrabar,
        },
    )


def _metrics(
    equity: pd.Series, trades: list[Trade], initial_cash: Decimal
) -> dict[str, float]:
    if equity.empty:
        return {}
    total_return = float(equity.iloc[-1]) / float(initial_cash) - 1.0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0

    # Sharpe from daily returns. Backtests use intraday bars; resample to
    # session-close equity so a single day is a single observation.
    daily = equity.resample("D").last().dropna().pct_change().dropna()
    if len(daily) > 1 and daily.std() > 0:
        sharpe = float(daily.mean() / daily.std() * (252**0.5))
    else:
        sharpe = 0.0

    if not trades:
        return {
            "total_return": total_return,
            "max_drawdown": max_dd,
            "sharpe": sharpe,
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
        }

    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")
    return {
        "total_return": total_return,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "num_trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": profit_factor,
    }
