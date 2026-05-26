"""A/B compare the composite strategy with vs without trailing stops.

Same window and universe as the work item 3 proxy comparison
(2025-12-01 -> 2026-02-28, SPY/NVDA/AAPL/JPM). Read-only: uses the local
parquet cache, never fetches fresh data.

A: trailing_stop_policy = {"orb": True, "pullback": True, "insider": False}
B: trailing_stop_policy = None  (control)

ORB and pullback use their production configs as-is. The pre-OR proxy is
left at its default (off). Insider on this 4-symbol universe usually
emits no signals; that is fine -- the script just reports zero.

Usage:
  uv run python scripts/compare_trailing.py
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import BarCache
from trading_bot.strategy import ORBStrategy, load_config
from trading_bot.strategy.composite import CompositeStrategy
from trading_bot.strategy.insider import InsiderStrategy
from trading_bot.strategy.insider import load_config as load_insider_config
from trading_bot.strategy.pullback import PullbackStrategy
from trading_bot.strategy.pullback import load_config as load_pullback_config

UNIVERSE = ["SPY", "NVDA", "AAPL", "JPM"]
START = "2025-12-01"
END = "2026-02-28"


def _build_strategy() -> CompositeStrategy:
    """Production composite: ORB + pullback + insider."""
    inner = []
    names: list[str] = []

    inner.append(ORBStrategy(load_config()))
    names.append("orb")

    inner.append(PullbackStrategy(load_pullback_config()))
    names.append("midday")

    try:
        inner.append(InsiderStrategy(load_insider_config()))
        names.append("insider")
    except Exception as exc:
        print(f"NOTE: insider strategy unavailable ({exc}); excluding.", file=sys.stderr)

    return CompositeStrategy(inner, names=names)


def _run_one(
    symbol: str,
    bars: pd.DataFrame,
    strategy: CompositeStrategy,
    policy: Mapping[str, bool] | None,
) -> dict:
    signals = strategy.generate_signals(bars)
    costs = BacktestCosts(
        commission_per_share=Decimal("0.005"),
        slippage_bps=Decimal("1.0"),
    )
    risk_params = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
    )
    result = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=risk_params,
        costs=costs,
        symbol=symbol,
        trailing_stop_policy=policy,
    )
    return {"signals": signals, "result": result}


def _aggregate(per_symbol: dict[str, dict]) -> dict:
    all_trades = []
    for sym, payload in per_symbol.items():
        for t in payload["result"].trades:
            all_trades.append(t)
    n = len(all_trades)
    if n == 0:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "gross_pnl": 0.0,
            "max_drawdown": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        }
    pnls = [float(t.pnl) for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    equity_pieces = []
    initial_total = Decimal("0")
    for sym, payload in per_symbol.items():
        eq = payload["result"].equity_curve
        if not eq.empty:
            equity_pieces.append(eq - 100000.0)
            initial_total += Decimal("100000")
    if equity_pieces:
        combined = (
            pd.concat(equity_pieces, axis=1, sort=True).ffill().fillna(0.0).sum(axis=1)
        )
        combined = combined + float(initial_total)
        peak = combined.cummax()
        dd = (combined / peak - 1.0).min()
    else:
        dd = 0.0

    return {
        "num_trades": n,
        "win_rate": len(wins) / n,
        "profit_factor": pf,
        "gross_pnl": sum(pnls),
        "max_drawdown": float(dd),
        "avg_win": (gross_wins / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
    }


def _per_symbol_summary(per_symbol: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sym, payload in per_symbol.items():
        trades = payload["result"].trades
        pnls = [float(t.pnl) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        n = len(pnls)
        gross_wins = sum(wins)
        gross_losses = abs(sum(losses))
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        eq = payload["result"].equity_curve
        if not eq.empty:
            peak = eq.cummax()
            dd = float((eq / peak - 1.0).min())
        else:
            dd = 0.0
        out[sym] = {
            "num_trades": n,
            "win_rate": (len(wins) / n) if n else 0.0,
            "profit_factor": pf,
            "gross_pnl": sum(pnls),
            "max_drawdown": dd,
        }
    return out


def _strategy_split(per_symbol: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sym, payload in per_symbol.items():
        for t in payload["result"].trades:
            key = t.strategy or "(none)"
            counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> int:
    start_date = datetime.strptime(START, "%Y-%m-%d").date()
    end_date = datetime.strptime(END, "%Y-%m-%d").date()
    start = datetime.combine(start_date - timedelta(days=1), time(0, 0), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)

    cache = BarCache(Path("data/bars"))

    policy_a = {"orb": True, "pullback": True, "insider": False}
    policy_b = None

    print(f"Period: {START} -> {END} (UTC)")
    print(f"Universe: {UNIVERSE}")
    print(f"A (trailing ON): policy={policy_a}")
    print(f"B (trailing OFF): policy={policy_b}")
    print()

    strat_a = _build_strategy()
    strat_b = _build_strategy()

    per_symbol_a: dict[str, dict] = {}
    per_symbol_b: dict[str, dict] = {}

    for sym in UNIVERSE:
        bars = cache.read(sym, start, end)
        if bars.empty:
            print(f"SKIP {sym}: no bars in cache for window", file=sys.stderr)
            continue
        bars = bars.copy()
        if "symbol" not in bars.columns:
            bars["symbol"] = sym
        per_symbol_a[sym] = _run_one(sym, bars, strat_a, policy_a)
        per_symbol_b[sym] = _run_one(sym, bars, strat_b, policy_b)
        print(
            f"{sym}: bars={len(bars)} "
            f"A_trades={len(per_symbol_a[sym]['result'].trades)} "
            f"B_trades={len(per_symbol_b[sym]['result'].trades)}"
        )

    print()
    agg_a = _aggregate(per_symbol_a)
    agg_b = _aggregate(per_symbol_b)

    print("=== Aggregate (universe-wide) ===")
    print(f"{'metric':<18} {'A (trail ON)':>16} {'B (trail OFF)':>16}")
    for k in [
        "num_trades",
        "win_rate",
        "profit_factor",
        "gross_pnl",
        "max_drawdown",
        "avg_win",
        "avg_loss",
    ]:
        va = agg_a[k]
        vb = agg_b[k]
        if isinstance(va, float):
            print(f"{k:<18} {va:>16.4f} {vb:>16.4f}")
        else:
            print(f"{k:<18} {va:>16} {vb:>16}")

    print()
    print("=== Strategy split (A run) ===")
    for name, count in sorted(_strategy_split(per_symbol_a).items()):
        print(f"  {name}: {count}")
    print("=== Strategy split (B run) ===")
    for name, count in sorted(_strategy_split(per_symbol_b).items()):
        print(f"  {name}: {count}")

    print()
    print("=== Per-symbol metrics ===")
    print(
        f"{'sym':<6} "
        f"{'A_n':>5} {'A_win%':>8} {'A_pf':>8} {'A_pnl':>12} {'A_mdd%':>8}   "
        f"{'B_n':>5} {'B_win%':>8} {'B_pf':>8} {'B_pnl':>12} {'B_mdd%':>8}"
    )
    sum_a = _per_symbol_summary(per_symbol_a)
    sum_b = _per_symbol_summary(per_symbol_b)
    for sym in UNIVERSE:
        if sym not in sum_a:
            continue
        sa = sum_a[sym]
        sb = sum_b[sym]
        print(
            f"{sym:<6} "
            f"{sa['num_trades']:>5} "
            f"{sa['win_rate']*100:>7.2f}% "
            f"{sa['profit_factor']:>8.3f} "
            f"{sa['gross_pnl']:>12.2f} "
            f"{sa['max_drawdown']*100:>7.2f}%   "
            f"{sb['num_trades']:>5} "
            f"{sb['win_rate']*100:>7.2f}% "
            f"{sb['profit_factor']:>8.3f} "
            f"{sb['gross_pnl']:>12.2f} "
            f"{sb['max_drawdown']*100:>7.2f}%"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
