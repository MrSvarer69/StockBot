"""Ablation: VWAP v2 with depth-only score (volume_decay disabled).

Re-runs the default config across the same universe/window and reports
Pearson(score, PnL), quartile win rates, and per-symbol PF — all with the
score replaced by `extension_depth` only (no volume term).

This isolates whether the volume_decay component is helping, hurting,
or noise. See risk #5 in the v2 brief.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import load_dotenv

# Pyth path hack so this is runnable from project root via `uv run`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import AlpacaBarFetcher, BarCache
from trading_bot.strategy.vwap.strategy import VWAPStrategy, load_config

ET = ZoneInfo("America/New_York")


def depth_only_score(closes, volumes, vwap_arr, atr_arr, *, ext_start, ext_end_exclusive):
    """Replacement for VWAPStrategy._score: depth only, no volume decay."""
    if ext_end_exclusive <= ext_start:
        return float("nan")
    depths = []
    for j in range(ext_start, ext_end_exclusive):
        a = atr_arr[j]
        if not np.isfinite(a) or a <= 0:
            continue
        depths.append(abs(closes[j] - vwap_arr[j]) / a)
    if not depths:
        return float("nan")
    return float(max(depths))


def main():
    load_dotenv()
    symbols = ["SPY", "NVDA", "AAPL", "JPM"]
    start_date = datetime(2025, 11, 15).date()
    end_date = datetime(2026, 5, 13).date()
    start = datetime.combine(start_date - timedelta(days=1), time(0, 0), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)

    cache = BarCache(Path("data/bars"))
    api_key = os.environ.get("ALPACA_API_KEY", "")
    api_secret = os.environ.get("ALPACA_API_SECRET", "")
    fetcher = (
        AlpacaBarFetcher(api_key=api_key, api_secret=api_secret)
        if api_key and api_secret else None
    )

    initial_cash = Decimal("100000")
    costs_main = BacktestCosts(
        commission_per_share=Decimal("0.005"),
        slippage_bps=Decimal("1.0"),
    )
    costs_stress = BacktestCosts(
        commission_per_share=Decimal("0.005"),
        slippage_bps=Decimal("3.0"),
    )
    risk_params = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
    )

    cfg = load_config()

    # Monkey-patch the score function on the class.
    original_score = VWAPStrategy._score
    VWAPStrategy._score = staticmethod(depth_only_score)

    try:
        pooled_scores: list[float] = []
        pooled_pnls: list[float] = []
        per_sym: list[dict] = []
        per_sym_stress: list[dict] = []
        for sym in symbols:
            if fetcher:
                bars = cache.read_or_fetch(sym, start, end, fetcher)
            else:
                bars = cache.read(sym, start, end)
            if bars.empty:
                continue
            if "symbol" not in bars.columns:
                bars = bars.copy()
                bars["symbol"] = sym

            strategy = VWAPStrategy(cfg)
            signals = strategy.generate_signals(bars)
            result = run_backtest(
                bars, signals, initial_cash=initial_cash,
                risk_params=risk_params, costs=costs_main, symbol=sym,
            )
            result_stress = run_backtest(
                bars, signals, initial_cash=initial_cash,
                risk_params=risk_params, costs=costs_stress, symbol=sym,
            )
            m = result.metrics
            ms = result_stress.metrics
            per_sym.append({
                "symbol": sym,
                "trades": int(m.get("num_trades", 0)),
                "win_rate": m.get("win_rate"),
                "profit_factor": m.get("profit_factor"),
                "total_return": m.get("total_return"),
            })
            per_sym_stress.append({
                "symbol": sym,
                "trades": int(ms.get("num_trades", 0)),
                "win_rate": ms.get("win_rate"),
                "profit_factor": ms.get("profit_factor"),
                "total_return": ms.get("total_return"),
            })

            # Pair scores with trade PnLs.
            entry_sigs = signals[signals["side"].isin(["long", "short"])][
                ["timestamp", "or_atr_ratio"]
            ]
            if entry_sigs.empty or not result.trades:
                continue
            sig_times = entry_sigs["timestamp"].to_numpy()
            sig_scores = entry_sigs["or_atr_ratio"].to_numpy()
            for t in result.trades:
                et_ts = pd.Timestamp(t.entry_time)
                idx = np.searchsorted(sig_times, et_ts, side="right") - 1
                if idx < 0:
                    continue
                delta = (et_ts - pd.Timestamp(sig_times[idx])).total_seconds()
                if 0 <= delta <= 120:
                    pooled_scores.append(float(sig_scores[idx]))
                    pooled_pnls.append(float(t.pnl))

        if pooled_scores:
            s = np.array(pooled_scores)
            o = np.array(pooled_pnls)
            corr = (
                float(np.corrcoef(s, o)[0, 1])
                if s.std() > 0 and o.std() > 0 else None
            )
            qs = np.quantile(s, [0.25, 0.5, 0.75])
            quart_wr = {}
            for q_lo, q_hi, label in [
                (-np.inf, qs[0], "Q1 (low)"),
                (qs[0], qs[1], "Q2"),
                (qs[1], qs[2], "Q3"),
                (qs[2], np.inf, "Q4 (high)"),
            ]:
                mask = (s > q_lo) & (s <= q_hi)
                oslice = o[mask]
                if len(oslice) > 0:
                    quart_wr[label] = float((oslice > 0).sum() / len(oslice))

            print("\n=== DEPTH-ONLY ABLATION (no volume_decay) ===")
            print(f"N paired observations: {len(pooled_scores)}")
            print(f"Pearson(score, PnL): {corr:.4f}" if corr is not None else "corr undefined")
            print("Win rate by score quartile:")
            for k, v in quart_wr.items():
                print(f"  {k}: {v*100:.2f}%")
            print("\nPer-symbol PF (1 bp):")
            for r in per_sym:
                pf = r["profit_factor"]
                pf_s = f"{pf:.3f}" if pf is not None else "n/a"
                tr = r["total_return"]
                tr_s = f"{tr*100:.3f}%" if tr is not None else "n/a"
                print(f"  {r['symbol']}: trades={r['trades']} pf={pf_s} tot_ret={tr_s}")
            print("\nPer-symbol PF (3 bps stress):")
            for r in per_sym_stress:
                pf = r["profit_factor"]
                pf_s = f"{pf:.3f}" if pf is not None else "n/a"
                print(f"  {r['symbol']}: trades={r['trades']} pf={pf_s}")
        else:
            print("No paired observations.")
    finally:
        VWAPStrategy._score = original_score


if __name__ == "__main__":
    main()
