"""Compare ORB with vs without the pre-OR proxy on the SAME bars.

Read-only on src/. Uses the standard backtest engine and the cached parquet
bars. Builds two ORBConfig instances (proxy on / off) and runs each across
the universe symbols, then prints aggregate metrics + entry-time split.

Usage:
  uv run python scripts/compare_proxy.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import BarCache
from trading_bot.strategy import ORBStrategy, load_config

ET = ZoneInfo("America/New_York")

UNIVERSE = ["SPY", "NVDA", "AAPL", "JPM"]
START = "2025-12-01"
END = "2026-02-28"


def _run_one(symbol: str, bars: pd.DataFrame, config) -> dict:
    strat = ORBStrategy(config)
    signals = strat.generate_signals(bars)
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

    # Aggregate equity curve: sum equity-minus-initial across symbols then back to
    # an aggregate equity series.
    equity_pieces = []
    initial_total = Decimal("0")
    for sym, payload in per_symbol.items():
        eq = payload["result"].equity_curve
        if not eq.empty:
            equity_pieces.append(eq - 100000.0)
            initial_total += Decimal("100000")
    if equity_pieces:
        combined = pd.concat(equity_pieces, axis=1, sort=True).ffill().fillna(0.0).sum(axis=1)
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


def _entry_time_split(per_symbol: dict[str, dict]) -> dict:
    proxy = 0  # entries in [09:30, 10:00) ET
    breakout = 0  # entries at/after 10:00 ET
    for sym, payload in per_symbol.items():
        for t in payload["result"].trades:
            et_t = t.entry_time.tz_convert(ET).time()
            if et_t < time(10, 0):
                proxy += 1
            else:
                breakout += 1
    return {"proxy": proxy, "breakout": breakout}


def main() -> int:
    start_date = datetime.strptime(START, "%Y-%m-%d").date()
    end_date = datetime.strptime(END, "%Y-%m-%d").date()
    start = datetime.combine(start_date - timedelta(days=1), time(0, 0), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)

    cache = BarCache(Path("data/bars"))

    cfg_default = load_config()
    cfg_a = replace(cfg_default, use_prior_close_proxy=True)
    cfg_b = replace(cfg_default, use_prior_close_proxy=False)

    print(f"Period: {START} -> {END} (UTC)")
    print(f"Universe: {UNIVERSE}")
    print(f"A (proxy ON): use_prior_close_proxy={cfg_a.use_prior_close_proxy}, pre_or_k={cfg_a.pre_or_k}")
    print(f"B (proxy OFF): use_prior_close_proxy={cfg_b.use_prior_close_proxy}")
    print()

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
        per_symbol_a[sym] = _run_one(sym, bars, cfg_a)
        per_symbol_b[sym] = _run_one(sym, bars, cfg_b)
        print(
            f"{sym}: bars={len(bars)} "
            f"A_trades={len(per_symbol_a[sym]['result'].trades)} "
            f"B_trades={len(per_symbol_b[sym]['result'].trades)}"
        )

    print()
    agg_a = _aggregate(per_symbol_a)
    agg_b = _aggregate(per_symbol_b)
    split_a = _entry_time_split(per_symbol_a)
    split_b = _entry_time_split(per_symbol_b)

    print("=== Aggregate (universe-wide) ===")
    print(f"{'metric':<18} {'A (proxy ON)':>18} {'B (proxy OFF)':>18}")
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
            print(f"{k:<18} {va:>18.4f} {vb:>18.4f}")
        else:
            print(f"{k:<18} {va:>18} {vb:>18}")

    print()
    print("=== Entry-time split (A run) ===")
    print(f"proxy-path (09:30-10:00 ET): {split_a['proxy']}")
    print(f"OR-breakout (>= 10:00 ET) : {split_a['breakout']}")
    print()
    print("=== Entry-time split (B run, sanity) ===")
    print(f"proxy-path : {split_b['proxy']} (should be 0)")
    print(f"OR-breakout: {split_b['breakout']}")

    print()
    print("=== Per-symbol metrics ===")
    print(f"{'sym':<6} {'A_n':>5} {'A_win%':>8} {'A_pf':>8} {'A_pnl':>12}   {'B_n':>5} {'B_win%':>8} {'B_pf':>8} {'B_pnl':>12}")
    for sym in UNIVERSE:
        if sym not in per_symbol_a:
            continue
        ma = per_symbol_a[sym]["result"].metrics
        mb = per_symbol_b[sym]["result"].metrics
        pnl_a = sum(float(t.pnl) for t in per_symbol_a[sym]["result"].trades)
        pnl_b = sum(float(t.pnl) for t in per_symbol_b[sym]["result"].trades)
        print(
            f"{sym:<6} "
            f"{ma.get('num_trades',0):>5} "
            f"{ma.get('win_rate',0)*100:>7.2f}% "
            f"{ma.get('profit_factor',0):>8.3f} "
            f"{pnl_a:>12.2f}   "
            f"{mb.get('num_trades',0):>5} "
            f"{mb.get('win_rate',0)*100:>7.2f}% "
            f"{mb.get('profit_factor',0):>8.3f} "
            f"{pnl_b:>12.2f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
