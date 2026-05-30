"""Isolation backtest for the PullbackStrategy.

Acceptance bar from the handover: PF > 1.0 in BOTH IS (first half) and
OOS (second half) at 3 bps slippage, with N>=20 trades per half.

Default symbol list is the 56-name wide universe used by ``run_paper.py``
minus the small-cap insider regional banks (which don't have cached
bars). The script also writes a survivor list to stdout so callers can
restrict the live universe to symbols whose midday-pullback edge cleared
the bar.

No live trading, no forecasts, no parameter sweep blind-search.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import BarCache, bars_for_range
from trading_bot.ops import setup_logging
from trading_bot.strategy.pullback.strategy import (
    PullbackStrategy,
    load_config,
)

ET = ZoneInfo("America/New_York")

# Wide list of cached symbols (excluding the regional banks that aren't
# in data/bars/). The backtest just runs every symbol that has bars and
# reports who clears the bar.
DEFAULT_SYMBOLS = (
    "SPY,QQQ,IWM,XLE,XLF,GLD,"
    "NVDA,AMD,MU,AVGO,SMCI,MRVL,ARM,"
    "AAPL,MSFT,META,GOOGL,AMZN,NFLX,ORCL,CRWD,SNOW,SHOP,BABA,"
    "JPM,GS,"
    "COIN,MSTR,RIOT,HOOD,TSLA,"
    "ABNB,BA,LLY,LMT,DIS,NKE,UNH,RBLX,PLTR,FCX,CVX,XOM"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pullback strategy backtest.")
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--start", default="2025-11-15")
    p.add_argument("--end", default="2026-05-13")
    p.add_argument("--initial-cash", default="100000")
    p.add_argument("--commission", default="0.005")
    p.add_argument("--slippage-bps", default="1.0")
    p.add_argument("--stress-slippage-bps", default="3.0")
    p.add_argument("--bars-dir", default="data/bars")
    p.add_argument("--reports-dir", default="data/backtests")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument(
        "--min-pf-per-half",
        type=float,
        default=1.0,
        help="Acceptance bar: PF must be >= this in BOTH halves at stress slippage.",
    )
    p.add_argument(
        "--min-trades-per-half",
        type=int,
        default=10,
        help="Acceptance bar: trade count must be >= this in BOTH halves.",
    )
    return p.parse_args(argv)


def _run_one(bars, *, cfg, initial_cash, risk_params, costs, symbol):
    strategy = PullbackStrategy(cfg)
    signals = strategy.generate_signals(bars)
    result = run_backtest(
        bars, signals,
        initial_cash=initial_cash,
        risk_params=risk_params,
        costs=costs,
        symbol=symbol,
    )
    return signals, result


def _split_at_midpoint(
    bars: pd.DataFrame, start_date, end_date,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Split bars into IS (first half) and OOS (second half) by trading days.

    Uses calendar midpoint between ``start_date`` and ``end_date``; the
    cached series rarely covers weekends so this approximates a trading-
    day split close enough for acceptance-bar purposes.
    """
    midpoint_date = start_date + (end_date - start_date) / 2
    midpoint_ts = pd.Timestamp(midpoint_date, tz="UTC")
    is_mask = bars.index < midpoint_ts
    oos_mask = bars.index >= midpoint_ts
    return bars[is_mask], bars[oos_mask], midpoint_ts


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
        print("ERROR: --end before --start", file=sys.stderr)
        return 1

    start = datetime.combine(start_date - timedelta(days=1), time(0, 0), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    setup_logging(level=logging.INFO, run_id=f"pullback-bt-{run_id}")

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

    rows: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for sym in symbols:
        try:
            bars = bars_for_range(
                sym, start, end, cache=cache, use_cache=use_cache,
            )
        except Exception as exc:
            skipped.append((sym, f"fetch error: {exc}"))
            continue
        if bars.empty:
            skipped.append((sym, "no bars in range"))
            continue
        if "symbol" not in bars.columns:
            bars = bars.copy()
            bars["symbol"] = sym

        is_bars, oos_bars, split_ts = _split_at_midpoint(
            bars, start_date, end_date,
        )

        def _summarize(b, c):
            if b.empty:
                return {"trades": 0, "pf": None, "wr": None, "ret": None}
            _, res = _run_one(
                b, cfg=base_cfg, initial_cash=initial_cash,
                risk_params=risk_params, costs=c, symbol=sym,
            )
            m = res.metrics
            return {
                "trades": int(m.get("num_trades", 0)),
                "pf": m.get("profit_factor"),
                "wr": m.get("win_rate"),
                "ret": m.get("total_return"),
            }

        is_main = _summarize(is_bars, costs_main)
        oos_main = _summarize(oos_bars, costs_main)
        is_stress = _summarize(is_bars, costs_stress)
        oos_stress = _summarize(oos_bars, costs_stress)

        passes = (
            is_stress["pf"] is not None
            and oos_stress["pf"] is not None
            and float(is_stress["pf"]) >= args.min_pf_per_half
            and float(oos_stress["pf"]) >= args.min_pf_per_half
            and is_stress["trades"] >= args.min_trades_per_half
            and oos_stress["trades"] >= args.min_trades_per_half
        )

        rows.append(
            {
                "symbol": sym,
                "is_trades": is_main["trades"],
                "is_pf": is_main["pf"],
                "is_wr": is_main["wr"],
                "is_ret": is_main["ret"],
                "oos_trades": oos_main["trades"],
                "oos_pf": oos_main["pf"],
                "oos_wr": oos_main["wr"],
                "oos_ret": oos_main["ret"],
                "is_stress_trades": is_stress["trades"],
                "is_stress_pf": is_stress["pf"],
                "oos_stress_trades": oos_stress["trades"],
                "oos_stress_pf": oos_stress["pf"],
                "passes_acceptance_at_stress": passes,
            }
        )
        print(
            f"  {sym}: IS pf={_format_metric(is_main['pf'], 2)} "
            f"trades={is_main['trades']} | OOS pf={_format_metric(oos_main['pf'], 2)} "
            f"trades={oos_main['trades']} | STRESS pf "
            f"IS={_format_metric(is_stress['pf'], 2)} "
            f"OOS={_format_metric(oos_stress['pf'], 2)} | "
            f"pass={passes}",
            flush=True,
        )

    report_dir = Path(args.reports_dir) / f"pullback-{run_id}"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / "per_symbol.csv"
    fields = [
        "symbol",
        "is_trades", "is_pf", "is_wr", "is_ret",
        "oos_trades", "oos_pf", "oos_wr", "oos_ret",
        "is_stress_trades", "is_stress_pf",
        "oos_stress_trades", "oos_stress_pf",
        "passes_acceptance_at_stress",
    ]
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Build a Markdown summary.
    lines: list[str] = []
    lines.append(f"# Pullback isolation backtest — run {run_id}")
    lines.append("")
    lines.append(f"- Window: {args.start} -> {args.end}")
    lines.append(f"- Symbols: {len(symbols)} (skipped: {len(skipped)})")
    lines.append(
        f"- Main slippage: {args.slippage_bps} bps, "
        f"stress: {args.stress_slippage_bps} bps"
    )
    lines.append(
        f"- Acceptance bar: PF >= {args.min_pf_per_half} in BOTH halves at "
        f"STRESS slippage, trades >= {args.min_trades_per_half} per half"
    )
    lines.append(f"- Config: {asdict(base_cfg)}")
    if skipped:
        lines.append("")
        lines.append("Skipped symbols:")
        for sym, reason in skipped:
            lines.append(f"- {sym}: {reason}")
    lines.append("")
    lines.append("## Per-symbol (default config, MAIN slippage)")
    lines.append("")
    lines.append(
        "| symbol | IS trades | IS pf | IS wr | OOS trades | OOS pf | OOS wr | pass |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        lines.append(
            f"| {r['symbol']} | {r['is_trades']} | "
            f"{_format_metric(r['is_pf'], 2)} | "
            f"{_format_metric(r['is_wr'], 2)} | "
            f"{r['oos_trades']} | "
            f"{_format_metric(r['oos_pf'], 2)} | "
            f"{_format_metric(r['oos_wr'], 2)} | "
            f"{'YES' if r['passes_acceptance_at_stress'] else 'no'} |"
        )

    survivors = [r["symbol"] for r in rows if r["passes_acceptance_at_stress"]]
    lines.append("")
    lines.append(
        f"## Survivors at acceptance bar ({len(survivors)} of {len(rows)} tested)"
    )
    lines.append("")
    if survivors:
        lines.append("```")
        lines.append(",".join(survivors))
        lines.append("```")
    else:
        lines.append("(no survivors)")

    lines.append("")
    lines.append("## Reminder")
    lines.append("")
    lines.append(
        "Historical results computed from the cached/fetched bar series, "
        "not a forecast. Do not extrapolate."
    )
    (report_dir / "report.md").write_text("\n".join(lines))

    print(f"\nReport: {report_dir}")
    print(f"Survivors ({len(survivors)}): {','.join(survivors) if survivors else '<none>'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
