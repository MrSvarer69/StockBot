"""Backtest the ORB strategy on historical Alpaca bars.

Usage:
  uv run python scripts/run_backtest.py --symbol SPY --start 2025-12-01 --end 2026-01-31
  uv run python scripts/run_backtest.py --symbol SPY --start 2026-01-15 --end 2026-01-15 --no-cache

Caches fetched bars to data/bars/<SYMBOL>/<DATE>.parquet so re-runs over the
same range are fast and offline-friendly.

Outputs a metrics summary to stdout and writes:
  data/backtests/<run-id>/report.md
  data/backtests/<run-id>/equity_curve.csv
  data/backtests/<run-id>/trades.csv
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import BacktestResult, RiskParams
from trading_bot.data import AlpacaBarFetcher, BarCache, ParquetBarFetcher
from trading_bot.ops import render_summary_line, render_trade_table, setup_logging
from trading_bot.strategy import ORBStrategy, load_config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the ORB strategy.")
    p.add_argument("--symbol", default="SPY", help="Symbol to backtest (default: SPY)")
    p.add_argument(
        "--start",
        required=True,
        help="Inclusive UTC date YYYY-MM-DD",
    )
    p.add_argument(
        "--end",
        required=True,
        help="Inclusive UTC date YYYY-MM-DD",
    )
    p.add_argument(
        "--initial-cash",
        type=str,
        default="100000",
        help="Starting cash for the simulation (default: 100000)",
    )
    p.add_argument(
        "--commission",
        type=str,
        default="0.005",
        help="Per-share commission, USD (default: 0.005)",
    )
    p.add_argument(
        "--slippage-bps",
        type=str,
        default="1.0",
        help="One-way slippage in basis points (default: 1.0)",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the local Parquet cache and always fetch from Alpaca.",
    )
    p.add_argument(
        "--bars-dir",
        default="data/bars",
        help="Root of the Parquet bar cache (default: data/bars)",
    )
    p.add_argument(
        "--reports-dir",
        default="data/backtests",
        help="Root of the backtest reports directory (default: data/backtests)",
    )
    return p.parse_args(argv)


def _bars_for_range(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    cache: BarCache,
    use_cache: bool,
):
    """Return the symbol's BarsFrame, populating the cache from Alpaca if needed."""
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
        # No credentials → cache-only mode (best-effort, may be incomplete).
        return cache.read(symbol, start, end)
    return cache.read_or_fetch(
        symbol,
        start,
        end,
        AlpacaBarFetcher(api_key=api_key, api_secret=api_secret),
    )


def _write_report(
    *,
    out_dir: Path,
    args: argparse.Namespace,
    result: BacktestResult,
) -> None:
    """Write a human-readable Markdown summary plus CSVs for trades + equity."""
    out_dir.mkdir(parents=True, exist_ok=True)

    equity_path = out_dir / "equity_curve.csv"
    result.equity_curve.to_csv(equity_path, header=True)

    trades_path = out_dir / "trades.csv"
    import csv

    with trades_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "symbol",
                "side",
                "entry_time",
                "exit_time",
                "entry_price",
                "exit_price",
                "qty",
                "pnl",
                "exit_reason",
            ]
        )
        for t in result.trades:
            w.writerow(
                [
                    t.symbol,
                    t.side,
                    t.entry_time,
                    t.exit_time,
                    t.entry_price,
                    t.exit_price,
                    t.qty,
                    t.pnl,
                    t.exit_reason,
                ]
            )

    lines: list[str] = []
    lines.append(f"# Backtest report — {args.symbol}")
    lines.append("")
    lines.append(f"- Range: {args.start} → {args.end} (UTC)")
    lines.append(f"- Initial cash: {args.initial_cash}")
    lines.append(f"- Commission per share: {args.commission}")
    lines.append(f"- Slippage (bps): {args.slippage_bps}")
    lines.append("")
    lines.append("## Metrics (historical, not a forecast)")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("| --- | --- |")
    for key in [
        "num_trades",
        "win_rate",
        "avg_win",
        "avg_loss",
        "profit_factor",
        "total_return",
        "max_drawdown",
        "sharpe",
        "skipped_signals",
    ]:
        if key in result.metrics:
            lines.append(f"| {key} | {result.metrics[key]} |")
    lines.append("")
    lines.append("## Artifacts")
    lines.append(f"- Equity curve: `{equity_path.name}`")
    lines.append(f"- Trades: `{trades_path.name}`")

    (out_dir / "report.md").write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end_date < start_date:
        print("ERROR: --end is before --start", file=sys.stderr)
        return 1

    # Pad by one calendar day on each side so the strategy gets prior-session
    # ATR continuity and we don't miss bars at the boundaries.
    start = datetime.combine(start_date - timedelta(days=1), time(0, 0), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    setup_logging(level=logging.INFO, run_id=f"backtest-{run_id}")

    cache = BarCache(Path(args.bars_dir))
    use_cache = not args.no_cache

    bars = _bars_for_range(
        args.symbol, start, end, cache=cache, use_cache=use_cache
    )
    if bars.empty:
        print(f"ERROR: no bars available for {args.symbol} in range", file=sys.stderr)
        return 1
    bars = bars.copy()
    if "symbol" not in bars.columns:
        bars["symbol"] = args.symbol.upper()

    strategy = ORBStrategy(load_config())
    signals = strategy.generate_signals(bars)

    initial_cash = Decimal(args.initial_cash)
    costs = BacktestCosts(
        commission_per_share=Decimal(args.commission),
        slippage_bps=Decimal(args.slippage_bps),
    )
    risk_params = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
    )

    result = run_backtest(
        bars,
        signals,
        initial_cash=initial_cash,
        risk_params=risk_params,
        costs=costs,
        symbol=args.symbol.upper(),
    )
    try:
        result.reconcile(initial_cash)
    except AssertionError as e:
        print(f"WARNING: reconciliation failed: {e}", file=sys.stderr)

    print(f"\n== Backtest {args.symbol} {args.start}..{args.end} ==")
    for key, value in result.metrics.items():
        print(f"  {key:20s} {value}")

    print()
    print(render_trade_table(result.trades))
    print(render_summary_line(result.trades))

    report_dir = Path(args.reports_dir) / run_id
    _write_report(out_dir=report_dir, args=args, result=result)
    print(f"\nReport written to: {report_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
