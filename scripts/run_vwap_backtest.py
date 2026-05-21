"""Isolation backtest for the VWAPStrategy.

Sister script to run_backtest.py (which is ORB-specific). Runs the VWAP
reclaim/rejection strategy across a basket of symbols over a configurable
window, with optional per-knob variants for the six open questions the
strategist flagged. Writes per-symbol metrics, signal-time distributions,
and a unified Markdown report to data/backtests/vwap-<run-id>/.

Honest-fill rules are inherited from trading_bot.backtest.run_backtest:
  - signals at bar t fill at bar t+1 open
  - costs (commission + slippage) charged on entry and exit
  - intrabar stop+take collision resolves pessimistically to the stop

No live trading, no forecasts, no parameter sweep blind-search. Variants
are picked deliberately to answer:

  Q1 confirm_bars: 2 (default) vs 3
  Q2 min_extension_bars: 5 (default) vs 3 and 8
  Q3 take_r_multiple: 1.5 (default) vs 1.0
  Q6 cooldown_minutes: 30 (default) vs 10 and 60

Q4 (per-symbol score behaviour) and Q5 (score vs outcome correlation) are
answered from the default-config run by aggregating trade-level data
across symbols, not by a parameter change.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import AlpacaBarFetcher, BarCache
from trading_bot.ops import setup_logging
from trading_bot.strategy.vwap.strategy import VWAPStrategy, load_config

ET = ZoneInfo("America/New_York")

DEFAULT_SYMBOLS = "SPY,NVDA,AAPL,JPM"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Isolation backtest of VWAPStrategy.")
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--start", default="2025-11-15")
    p.add_argument("--end", default="2026-05-13")
    p.add_argument("--initial-cash", default="100000")
    p.add_argument("--commission", default="0.005")
    p.add_argument("--slippage-bps", default="1.0")
    p.add_argument("--bars-dir", default="data/bars")
    p.add_argument("--reports-dir", default="data/backtests")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument(
        "--no-variants",
        action="store_true",
        help="Skip the parameter variants; default-config only.",
    )
    p.add_argument(
        "--stress-slippage-bps",
        default="3.0",
        help="Additional pessimistic-slippage stress run at this bps level.",
    )
    return p.parse_args(argv)


def _bars_for_range(
    symbol: str, start: datetime, end: datetime, *, cache: BarCache, use_cache: bool
):
    api_key = os.environ.get("ALPACA_API_KEY", "")
    api_secret = os.environ.get("ALPACA_API_SECRET", "")
    if not use_cache:
        if not api_key or not api_secret:
            raise SystemExit(
                "ERROR: ALPACA_API_KEY/ALPACA_API_SECRET required to fetch bars"
            )
        return AlpacaBarFetcher(api_key=api_key, api_secret=api_secret).fetch(
            symbol, start, end
        )
    if not api_key or not api_secret:
        return cache.read(symbol, start, end)
    return cache.read_or_fetch(
        symbol,
        start,
        end,
        AlpacaBarFetcher(api_key=api_key, api_secret=api_secret),
    )


def _run_one(
    *,
    symbol: str,
    bars: pd.DataFrame,
    config,
    initial_cash: Decimal,
    risk_params: RiskParams,
    costs: BacktestCosts,
):
    strategy = VWAPStrategy(config)
    signals = strategy.generate_signals(bars)
    result = run_backtest(
        bars,
        signals,
        initial_cash=initial_cash,
        risk_params=risk_params,
        costs=costs,
        symbol=symbol,
    )
    return signals, result


def _signal_hour_distribution(signals: pd.DataFrame) -> dict[int, int]:
    """Bucket non-flat signals by ET hour. Returns dict {hour: count}."""
    if signals.empty:
        return {}
    entry = signals[signals["side"].isin(["long", "short"])]
    if entry.empty:
        return {}
    et = entry["timestamp"].dt.tz_convert(ET)
    return et.dt.hour.value_counts().sort_index().to_dict()


def _signals_per_session(signals: pd.DataFrame) -> float:
    if signals.empty:
        return 0.0
    entry = signals[signals["side"].isin(["long", "short"])]
    if entry.empty:
        return 0.0
    et_date = entry["timestamp"].dt.tz_convert(ET).dt.date
    return float(len(entry) / max(1, et_date.nunique()))


def _trading_sessions(bars: pd.DataFrame) -> int:
    if bars.empty:
        return 0
    et = bars.index.tz_convert(ET)
    return pd.Series(et.date).nunique()


def _take_fill_rate(result, signals_count: int) -> tuple[float, dict]:
    """Fraction of trades that hit take vs stop vs time/session_end."""
    if not result.trades:
        return 0.0, {}
    reasons = {}
    for t in result.trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    take = reasons.get("take", 0)
    return (take / len(result.trades)), reasons


def _score_outcome_correlation(signals: pd.DataFrame, trades) -> float | None:
    """Pearson correlation between score (|close-vwap|/atr_bar) and trade PnL.

    Aligns each trade to its entry signal by entry_time. Returns None if
    fewer than 5 paired observations (correlation not interpretable).
    """
    if signals.empty or not trades:
        return None
    sig = signals[signals["side"].isin(["long", "short"])][
        ["timestamp", "or_atr_ratio"]
    ].rename(columns={"timestamp": "signal_ts", "or_atr_ratio": "score"})
    # The trade entry_time is the FILL bar (signal bar + 1). Match by the
    # nearest preceding signal within ~2 minutes.
    pairs: list[tuple[float, float]] = []
    sig_sorted = sig.sort_values("signal_ts").reset_index(drop=True)
    sig_times = sig_sorted["signal_ts"].to_numpy()
    sig_scores = sig_sorted["score"].to_numpy()
    for t in trades:
        et_ts = pd.Timestamp(t.entry_time)
        # Find latest signal_ts <= et_ts
        idxs = np.searchsorted(sig_times, et_ts, side="right") - 1
        if idxs < 0:
            continue
        delta = (et_ts - pd.Timestamp(sig_times[idxs])).total_seconds()
        if 0 <= delta <= 120:  # within 2 min
            pairs.append((float(sig_scores[idxs]), float(t.pnl)))
    if len(pairs) < 5:
        return None
    s = np.array([p[0] for p in pairs])
    o = np.array([p[1] for p in pairs])
    if s.std() == 0 or o.std() == 0:
        return None
    return float(np.corrcoef(s, o)[0, 1])


def _entry_dates(signals: pd.DataFrame) -> set:
    if signals.empty:
        return set()
    entry = signals[signals["side"].isin(["long", "short"])]
    if entry.empty:
        return set()
    return set(entry["timestamp"].dt.tz_convert(ET).dt.date.unique())


def _bar_atr_stats(bars: pd.DataFrame, period: int = 14) -> dict[str, float]:
    """Summary stats on per-bar SMA-ATR over the bars. Used for Q4."""
    if bars.empty:
        return {}
    et = bars.index.tz_convert(ET)
    rth_mask = pd.Series(
        ((et.time >= time(9, 30)) & (et.time < time(16, 0))), index=bars.index
    )
    rth = bars[rth_mask.to_numpy()]
    if rth.empty:
        return {}
    high = rth["high"]
    low = rth["low"]
    prev_close = rth["close"].shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=period, min_periods=1).mean()
    close = rth["close"].astype("float64")
    atr_pct = (atr / close).dropna()
    return {
        "atr_mean": float(atr.mean()),
        "atr_median": float(atr.median()),
        "atr_pct_mean": float(atr_pct.mean()),
        "atr_pct_median": float(atr_pct.median()),
        "atr_pct_std": float(atr_pct.std()),
    }


def _run_orb_comparison(symbol: str, bars: pd.DataFrame, initial_cash, risk_params, costs):
    """Run the same ORB config used in the prior screen for entry-date overlap."""
    from trading_bot.strategy import ORBStrategy, load_config as load_orb

    strategy = ORBStrategy(load_orb())
    signals = strategy.generate_signals(bars)
    result = run_backtest(
        bars, signals, initial_cash=initial_cash,
        risk_params=risk_params, costs=costs, symbol=symbol,
    )
    return signals, result


def _format_metric(v, decimals: int = 4) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end_date < start_date:
        print("ERROR: --end is before --start", file=sys.stderr)
        return 1

    start = datetime.combine(start_date - timedelta(days=1), time(0, 0), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    setup_logging(level=logging.INFO, run_id=f"vwap-bt-{run_id}")

    cache = BarCache(Path(args.bars_dir))
    use_cache = not args.no_cache

    initial_cash = Decimal(args.initial_cash)
    costs_main = BacktestCosts(
        commission_per_share=Decimal(args.commission),
        slippage_bps=Decimal(args.slippage_bps),
    )
    costs_stress = BacktestCosts(
        commission_per_share=Decimal(args.commission),
        slippage_bps=Decimal(args.stress_slippage_bps),
    )
    risk_params = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
    )

    base_cfg = load_config()

    # Variant definitions — one tweak vs default each.
    if args.no_variants:
        variants = {"default": base_cfg}
    else:
        variants = {
            "default": base_cfg,
            "confirm_bars=3": replace(base_cfg, confirm_bars=3),
            "min_ext_bars=3": replace(base_cfg, min_extension_bars=3),
            "min_ext_bars=8": replace(base_cfg, min_extension_bars=8),
            "take_r=1.0": replace(base_cfg, take_r_multiple=1.0),
            "cooldown=10": replace(base_cfg, cooldown_minutes=10),
            "cooldown=60": replace(base_cfg, cooldown_minutes=60),
        }

    # --- Load bars once per symbol. ---
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    skipped: list[tuple[str, str]] = []
    for sym in symbols:
        print(f"  loading bars {sym} ...", flush=True)
        try:
            bars = _bars_for_range(
                sym, start, end, cache=cache, use_cache=use_cache,
            )
        except Exception as exc:
            skipped.append((sym, f"fetch error: {exc}"))
            continue
        if bars.empty:
            skipped.append((sym, "no bars in range"))
            continue
        bars = bars.copy()
        if "symbol" not in bars.columns:
            bars["symbol"] = sym
        bars_by_symbol[sym] = bars

    if not bars_by_symbol:
        print("ERROR: no symbols had bars; aborting.", file=sys.stderr)
        return 1

    report_dir = Path(args.reports_dir) / f"vwap-{run_id}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # --- Per-symbol default-config + ORB overlap + stress run. ---
    default_rows: list[dict] = []
    stress_rows: list[dict] = []
    orb_rows: list[dict] = []
    all_default_trades: list = []
    all_default_signals_frames: list[pd.DataFrame] = []
    vwap_entry_dates_by_sym: dict[str, set] = {}
    orb_entry_dates_by_sym: dict[str, set] = {}
    hour_dist_aggregate: dict[int, int] = {}
    bar_atr_stats: dict[str, dict[str, float]] = {}

    for sym, bars in bars_by_symbol.items():
        sessions = _trading_sessions(bars)
        bar_atr_stats[sym] = _bar_atr_stats(bars, period=base_cfg.atr_period_bars)

        # Default config, main-slippage.
        signals, result = _run_one(
            symbol=sym, bars=bars, config=base_cfg,
            initial_cash=initial_cash, risk_params=risk_params, costs=costs_main,
        )
        sigs_per = _signals_per_session(signals)
        take_rate, reasons = _take_fill_rate(result, len(signals))
        hours = _signal_hour_distribution(signals)
        for h, c in hours.items():
            hour_dist_aggregate[h] = hour_dist_aggregate.get(h, 0) + c
        vwap_entry_dates_by_sym[sym] = _entry_dates(signals)
        all_default_trades.extend([(sym, t, signals) for t in result.trades])
        all_default_signals_frames.append(signals.assign(_sym=sym))

        m = result.metrics
        default_rows.append(
            {
                "symbol": sym,
                "sessions": sessions,
                "trades": int(m.get("num_trades", 0)),
                "win_rate": m.get("win_rate"),
                "profit_factor": m.get("profit_factor"),
                "total_return": m.get("total_return"),
                "max_drawdown": m.get("max_drawdown"),
                "sharpe": m.get("sharpe"),
                "signals_per_session": sigs_per,
                "take_rate": take_rate,
                "exit_reasons": reasons,
                "avg_win": m.get("avg_win"),
                "avg_loss": m.get("avg_loss"),
                "skipped": m.get("skipped_signals", 0),
            }
        )
        # Stress slippage.
        _, result_stress = _run_one(
            symbol=sym, bars=bars, config=base_cfg,
            initial_cash=initial_cash, risk_params=risk_params, costs=costs_stress,
        )
        ms = result_stress.metrics
        stress_rows.append(
            {
                "symbol": sym,
                "trades": int(ms.get("num_trades", 0)),
                "win_rate": ms.get("win_rate"),
                "profit_factor": ms.get("profit_factor"),
                "total_return": ms.get("total_return"),
                "max_drawdown": ms.get("max_drawdown"),
                "sharpe": ms.get("sharpe"),
            }
        )

        # ORB comparison.
        try:
            orb_sigs, orb_res = _run_orb_comparison(
                sym, bars, initial_cash, risk_params, costs_main,
            )
            orb_entry_dates_by_sym[sym] = _entry_dates(orb_sigs)
            ms = orb_res.metrics
            orb_rows.append(
                {
                    "symbol": sym,
                    "trades": int(ms.get("num_trades", 0)),
                    "win_rate": ms.get("win_rate"),
                    "profit_factor": ms.get("profit_factor"),
                    "total_return": ms.get("total_return"),
                }
            )
        except Exception as exc:
            print(f"  WARN: ORB compare failed for {sym}: {exc}")

    # --- Variant rollups across all symbols. ---
    variant_summary: dict[str, list[dict]] = {}
    for vname, vcfg in variants.items():
        if vname == "default":
            variant_summary[vname] = default_rows  # reuse
            continue
        rows: list[dict] = []
        for sym, bars in bars_by_symbol.items():
            sigs, res = _run_one(
                symbol=sym, bars=bars, config=vcfg,
                initial_cash=initial_cash, risk_params=risk_params,
                costs=costs_main,
            )
            m = res.metrics
            rows.append(
                {
                    "symbol": sym,
                    "trades": int(m.get("num_trades", 0)),
                    "win_rate": m.get("win_rate"),
                    "profit_factor": m.get("profit_factor"),
                    "total_return": m.get("total_return"),
                    "max_drawdown": m.get("max_drawdown"),
                    "sharpe": m.get("sharpe"),
                    "signals_per_session": _signals_per_session(sigs),
                    "take_rate": _take_fill_rate(res, len(sigs))[0],
                }
            )
        variant_summary[vname] = rows

    # --- Score vs outcome correlation, pooled across symbols (Q5). ---
    pooled_scores: list[float] = []
    pooled_pnls: list[float] = []
    for sym, bars in bars_by_symbol.items():
        sigs, res = _run_one(
            symbol=sym, bars=bars, config=base_cfg,
            initial_cash=initial_cash, risk_params=risk_params, costs=costs_main,
        )
        if not res.trades or sigs.empty:
            continue
        entry_sigs = sigs[sigs["side"].isin(["long", "short"])][
            ["timestamp", "or_atr_ratio"]
        ]
        if entry_sigs.empty:
            continue
        sig_times = entry_sigs["timestamp"].to_numpy()
        sig_scores = entry_sigs["or_atr_ratio"].to_numpy()
        for t in res.trades:
            et_ts = pd.Timestamp(t.entry_time)
            idx = np.searchsorted(sig_times, et_ts, side="right") - 1
            if idx < 0:
                continue
            delta = (et_ts - pd.Timestamp(sig_times[idx])).total_seconds()
            if 0 <= delta <= 120:
                pooled_scores.append(float(sig_scores[idx]))
                pooled_pnls.append(float(t.pnl))
    if len(pooled_scores) >= 5:
        s = np.array(pooled_scores)
        o = np.array(pooled_pnls)
        score_corr = float(np.corrcoef(s, o)[0, 1]) if s.std() > 0 and o.std() > 0 else None
        # Quartile-binned win rates for a non-linear sanity check.
        qs = np.quantile(s, [0.25, 0.5, 0.75])
        quartile_winrate: dict[str, float] = {}
        for q_lo, q_hi, label in [
            (-np.inf, qs[0], "Q1 (low)"),
            (qs[0], qs[1], "Q2"),
            (qs[1], qs[2], "Q3"),
            (qs[2], np.inf, "Q4 (high)"),
        ]:
            mask = (s > q_lo) & (s <= q_hi)
            o_slice = o[mask]
            if len(o_slice) > 0:
                quartile_winrate[label] = float((o_slice > 0).sum() / len(o_slice))
    else:
        score_corr = None
        quartile_winrate = {}

    # --- Overlap analysis (Q5 of the brief): VWAP vs ORB entry dates. ---
    overlap_rows: list[dict] = []
    for sym in bars_by_symbol:
        vwap_dates = vwap_entry_dates_by_sym.get(sym, set())
        orb_dates = orb_entry_dates_by_sym.get(sym, set())
        overlap = vwap_dates & orb_dates
        overlap_rows.append(
            {
                "symbol": sym,
                "vwap_days": len(vwap_dates),
                "orb_days": len(orb_dates),
                "overlap_days": len(overlap),
                "vwap_only_days": len(vwap_dates - orb_dates),
                "orb_only_days": len(orb_dates - vwap_dates),
            }
        )

    # --- Write CSVs. ---
    def _write_csv(name: str, rows: list[dict], fields: list[str]) -> Path:
        path = report_dir / name
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    default_csv = _write_csv(
        "default_per_symbol.csv",
        default_rows,
        ["symbol", "sessions", "trades", "win_rate", "profit_factor",
         "total_return", "max_drawdown", "sharpe", "signals_per_session",
         "take_rate", "avg_win", "avg_loss", "skipped"],
    )
    stress_csv = _write_csv(
        "stress_per_symbol.csv",
        stress_rows,
        ["symbol", "trades", "win_rate", "profit_factor", "total_return",
         "max_drawdown", "sharpe"],
    )
    orb_csv = _write_csv(
        "orb_comparison.csv",
        orb_rows,
        ["symbol", "trades", "win_rate", "profit_factor", "total_return"],
    )
    overlap_csv = _write_csv(
        "vwap_vs_orb_overlap.csv",
        overlap_rows,
        ["symbol", "vwap_days", "orb_days", "overlap_days",
         "vwap_only_days", "orb_only_days"],
    )
    for vname, rows in variant_summary.items():
        if vname == "default":
            continue
        _write_csv(
            f"variant_{vname.replace('=', '_')}.csv",
            rows,
            ["symbol", "trades", "win_rate", "profit_factor", "total_return",
             "max_drawdown", "sharpe", "signals_per_session", "take_rate"],
        )

    # --- Build the Markdown report. ---
    def fmt_pct(v):
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except (TypeError, ValueError):
            return "-"

    lines: list[str] = []
    lines.append(f"# VWAP isolation backtest — run {run_id}")
    lines.append("")
    lines.append(f"- Window: {args.start} -> {args.end} (UTC dates, inclusive)")
    lines.append(f"- Symbols: {', '.join(symbols)}")
    lines.append(f"- Main slippage: {args.slippage_bps} bps, stress: {args.stress_slippage_bps} bps")
    lines.append(f"- Commission/share: {args.commission}")
    lines.append(f"- Initial cash: {args.initial_cash}")
    lines.append(f"- Strategy default config: {asdict(base_cfg)}")
    if skipped:
        lines.append("")
        lines.append("Symbols skipped:")
        for sym, reason in skipped:
            lines.append(f"- {sym}: {reason}")
    lines.append("")
    lines.append("## 1. Headline metrics (default config, main slippage)")
    lines.append("")
    lines.append("| symbol | sessions | trades | win% | pf | tot_return | max_dd | sharpe | sigs/sess | take_rate | skipped |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in default_rows:
        lines.append(
            f"| {r['symbol']} | {r['sessions']} | {r['trades']} | "
            f"{fmt_pct(r['win_rate'])} | {_format_metric(r['profit_factor'], 2)} | "
            f"{fmt_pct(r['total_return'])} | {fmt_pct(r['max_drawdown'])} | "
            f"{_format_metric(r['sharpe'], 2)} | "
            f"{_format_metric(r['signals_per_session'], 2)} | "
            f"{fmt_pct(r['take_rate'])} | {r['skipped']} |"
        )

    lines.append("")
    lines.append("## 2. Signal-rate distribution by ET hour (default config, all symbols)")
    lines.append("")
    if hour_dist_aggregate:
        lines.append("| ET hour | entries |")
        lines.append("| --- | --- |")
        for h in sorted(hour_dist_aggregate):
            lines.append(f"| {h:02d}:00-{h:02d}:59 | {hour_dist_aggregate[h]} |")
    else:
        lines.append("(no entries)")

    lines.append("")
    lines.append("## 3. Exit-reason breakdown (default config)")
    lines.append("")
    lines.append("| symbol | take | stop | time | other |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in default_rows:
        er = r["exit_reasons"] or {}
        lines.append(
            f"| {r['symbol']} | {er.get('take', 0)} | {er.get('stop', 0)} | "
            f"{er.get('time', 0)} | "
            f"{sum(v for k, v in er.items() if k not in ('take','stop','time'))} |"
        )

    lines.append("")
    lines.append("## 4. Stress run at higher slippage")
    lines.append("")
    lines.append(f"Slippage = {args.stress_slippage_bps} bps")
    lines.append("")
    lines.append("| symbol | trades | win% | pf | tot_return | max_dd | sharpe |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in stress_rows:
        lines.append(
            f"| {r['symbol']} | {r['trades']} | {fmt_pct(r['win_rate'])} | "
            f"{_format_metric(r['profit_factor'], 2)} | "
            f"{fmt_pct(r['total_return'])} | {fmt_pct(r['max_drawdown'])} | "
            f"{_format_metric(r['sharpe'], 2)} |"
        )

    lines.append("")
    lines.append("## 5. Parameter variants (pooled per-symbol, main slippage)")
    lines.append("")
    for vname, rows in variant_summary.items():
        if vname == "default":
            continue
        lines.append(f"### {vname}")
        lines.append("")
        lines.append("| symbol | trades | win% | pf | tot_return | sigs/sess | take_rate |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            lines.append(
                f"| {r['symbol']} | {r['trades']} | "
                f"{fmt_pct(r['win_rate'])} | "
                f"{_format_metric(r['profit_factor'], 2)} | "
                f"{fmt_pct(r['total_return'])} | "
                f"{_format_metric(r.get('signals_per_session'), 2)} | "
                f"{fmt_pct(r.get('take_rate'))} |"
            )
        lines.append("")

    lines.append("## 6. ORB comparison + entry-date overlap (same window/symbols)")
    lines.append("")
    lines.append("### ORB on this universe (default ORB config)")
    lines.append("")
    lines.append("| symbol | trades | win% | pf | tot_return |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in orb_rows:
        lines.append(
            f"| {r['symbol']} | {r['trades']} | {fmt_pct(r['win_rate'])} | "
            f"{_format_metric(r['profit_factor'], 2)} | "
            f"{fmt_pct(r['total_return'])} |"
        )
    lines.append("")
    lines.append("### Entry-date overlap")
    lines.append("")
    lines.append("| symbol | vwap_days | orb_days | both | vwap_only | orb_only |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in overlap_rows:
        lines.append(
            f"| {r['symbol']} | {r['vwap_days']} | {r['orb_days']} | "
            f"{r['overlap_days']} | {r['vwap_only_days']} | {r['orb_only_days']} |"
        )

    lines.append("")
    lines.append("## 7. Score (|close-vwap|/atr_bar) vs outcome (default config, pooled)")
    lines.append("")
    if score_corr is not None:
        lines.append(f"- Pearson correlation (score, PnL): {score_corr:.4f}")
        lines.append(f"- N paired observations: {len(pooled_scores)}")
        if quartile_winrate:
            lines.append("")
            lines.append("Win rate by score quartile:")
            lines.append("")
            lines.append("| quartile | win_rate |")
            lines.append("| --- | --- |")
            for k, v in quartile_winrate.items():
                lines.append(f"| {k} | {fmt_pct(v)} |")
    else:
        lines.append("- Not enough paired observations to compute correlation.")

    lines.append("")
    lines.append("## 8. Per-symbol bar-ATR profile (Q4)")
    lines.append("")
    lines.append("| symbol | atr_mean | atr_median | atr_pct_mean | atr_pct_std |")
    lines.append("| --- | --- | --- | --- | --- |")
    for sym, s in bar_atr_stats.items():
        if not s:
            continue
        lines.append(
            f"| {sym} | {_format_metric(s['atr_mean'], 4)} | "
            f"{_format_metric(s['atr_median'], 4)} | "
            f"{_format_metric(s['atr_pct_mean'] * 100, 4)}% | "
            f"{_format_metric(s['atr_pct_std'] * 100, 4)}% |"
        )

    lines.append("")
    lines.append("## Reminder")
    lines.append("")
    lines.append("Historical results computed from the cached/fetched bar series, "
                 "not a forecast. Do not extrapolate.")

    (report_dir / "report.md").write_text("\n".join(lines))
    print(f"\nReport written to: {report_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
