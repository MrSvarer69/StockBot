"""Sweep an ORB strategy parameter over a list of values.

Usage:
  uv run python scripts/sweep_orb.py \
      --symbol SPY --start 2026-05-07 --end 2026-05-13 \
      --param min_range_atr_multiplier --values 0.3,0.5,0.7,1.0,1.5

Reuses the same Parquet bar cache as run_backtest.py. Each value is run
through the same engine and the metrics are printed as a comparison table.

Historical results only — never extrapolated, never a forecast.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import BarCache, bars_for_range
from trading_bot.ops import setup_logging
from trading_bot.strategy import ORBStrategy, load_config
from trading_bot.strategy.orb.strategy import ORBConfig


_SWEEPABLE_FIELDS = {
    "min_range_atr_multiplier",
    "atr_stop_multiplier",
    "take_r_multiple",
    "target_size_pct",
    "opening_range_minutes",
    "atr_period_sessions",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep an ORB parameter and compare metrics.")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--start", required=True, help="Inclusive UTC date YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Inclusive UTC date YYYY-MM-DD")
    p.add_argument(
        "--param",
        default="min_range_atr_multiplier",
        choices=sorted(_SWEEPABLE_FIELDS),
        help="ORB config field to vary (default: min_range_atr_multiplier)",
    )
    p.add_argument(
        "--values",
        required=True,
        help="Comma-separated values to try, e.g. '0.3,0.5,0.7,1.0'",
    )
    p.add_argument("--initial-cash", default="100000")
    p.add_argument("--commission", default="0.005")
    p.add_argument("--slippage-bps", default="1.0")
    p.add_argument("--bars-dir", default="data/bars")
    p.add_argument("--no-cache", action="store_true")
    return p.parse_args(argv)


def _coerce(value_str: str, field_name: str):
    """Cast a sweep value to the right type for the ORBConfig field."""
    if field_name in {"opening_range_minutes", "atr_period_sessions"}:
        return int(value_str)
    return float(value_str)


def _format_metric(value, decimals: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def _render_sweep_table(
    param: str, rows: list[tuple[object, dict[str, float]]]
) -> str:
    """Render the sweep comparison as a Unicode table."""
    headers = [
        param,
        "trades",
        "win%",
        "avg_win",
        "avg_loss",
        "pf",
        "tot_return%",
        "max_dd%",
        "sharpe",
    ]
    body: list[list[str]] = []
    for value, m in rows:
        body.append(
            [
                str(value),
                str(int(m.get("num_trades", 0))),
                _format_metric(m.get("win_rate", 0) * 100, decimals=1),
                _format_metric(m.get("avg_win"), decimals=2),
                _format_metric(m.get("avg_loss"), decimals=2),
                _format_metric(m.get("profit_factor"), decimals=2),
                _format_metric(m.get("total_return", 0) * 100, decimals=3),
                _format_metric(m.get("max_drawdown", 0) * 100, decimals=3),
                _format_metric(m.get("sharpe"), decimals=2),
            ]
        )
    widths = [len(h) for h in headers]
    for row in body:
        for j, cell in enumerate(row):
            if len(cell) > widths[j]:
                widths[j] = len(cell)

    def hbar(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def render_row(row, center: bool):
        out = []
        for cell, w in zip(row, widths):
            out.append(f" {cell:^{w}} " if center else f" {cell:>{w}} ")
        return "│" + "│".join(out) + "│"

    lines = [hbar("┌", "┬", "┐"), render_row(headers, center=True), hbar("├", "┼", "┤")]
    for i, row in enumerate(body):
        lines.append(render_row(row, center=False))
        if i < len(body) - 1:
            lines.append(hbar("├", "┼", "┤"))
    lines.append(hbar("└", "┴", "┘"))
    return "\n".join(lines)


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

    raw_values = [v.strip() for v in args.values.split(",") if v.strip()]
    values = [_coerce(v, args.param) for v in raw_values]

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    setup_logging(level=logging.WARNING, run_id=f"sweep-{run_id}")

    cache = BarCache(Path(args.bars_dir))
    use_cache = not args.no_cache
    bars = bars_for_range(args.symbol, start, end, cache=cache, use_cache=use_cache)
    if bars.empty:
        print(f"ERROR: no bars for {args.symbol} in range", file=sys.stderr)
        return 1
    bars = bars.copy()
    if "symbol" not in bars.columns:
        bars["symbol"] = args.symbol.upper()

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

    base_cfg: ORBConfig = load_config()
    print(
        f"\n== Sweep {args.param} on {args.symbol} {args.start}..{args.end} =="
    )
    print(f"   values: {raw_values}")
    print(f"   baseline {args.param}: {getattr(base_cfg, args.param)}")
    print()

    rows: list[tuple[object, dict[str, float]]] = []
    for value in values:
        cfg = replace(base_cfg, **{args.param: value})
        strategy = ORBStrategy(cfg)
        signals = strategy.generate_signals(bars)
        result = run_backtest(
            bars,
            signals,
            initial_cash=initial_cash,
            risk_params=risk_params,
            costs=costs,
            symbol=args.symbol.upper(),
        )
        rows.append((value, dict(result.metrics)))

    print(_render_sweep_table(args.param, rows))
    print()
    print("Reminder: historical results, not a forecast. Pick a value that")
    print("survives an out-of-sample window before relying on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
