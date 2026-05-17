"""Screen a list of symbols by running the same ORB config across each.

Usage:
  uv run python scripts/screen_symbols.py \
      --symbols SPY,QQQ,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL,AMZN \
      --start 2025-11-15 --end 2026-05-13

Companion to sweep_orb.py: that script varies an ORB parameter on a single
symbol; this one holds the ORB config fixed and varies the symbol. Reuses
the same engine and Parquet bar cache.

Historical results only — never extrapolated, never a forecast.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys

logger = logging.getLogger(__name__)
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import AlpacaBarFetcher, BarCache
from trading_bot.ops import setup_logging
from trading_bot.strategy import ORBStrategy, load_config


_DEFAULT_SYMBOLS = "SPY,QQQ,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL,AMZN"

# Symbols with fewer than this many ORB trades over the screen window are
# placed in the "INSUFFICIENT" group at the bottom of the leaderboard and
# excluded from the headline ranking. ORB triggers at most once per day per
# symbol, so a 6-month window can plausibly produce ~10-40 trades depending
# on volatility — below 10, the metrics are too noisy to rank on.
_MIN_TRADES_FOR_RANK = 10


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Screen a basket of symbols against a fixed ORB config."
    )
    p.add_argument(
        "--symbols",
        default=_DEFAULT_SYMBOLS,
        help=f"Comma-separated list of symbols (default: {_DEFAULT_SYMBOLS})",
    )
    p.add_argument("--start", required=True, help="Inclusive UTC date YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Inclusive UTC date YYYY-MM-DD")
    p.add_argument("--initial-cash", default="100000")
    p.add_argument("--commission", default="0.005")
    p.add_argument("--slippage-bps", default="1.0")
    p.add_argument("--bars-dir", default="data/bars")
    p.add_argument("--reports-dir", default="data/backtests")
    p.add_argument("--no-cache", action="store_true")
    return p.parse_args(argv)


def _bars_for_range(symbol: str, start, end, *, cache: BarCache, use_cache: bool):
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


def _format_metric(value, decimals: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def _render_screen_table(
    ranked: list[tuple[str, dict[str, float]]],
    insufficient: list[tuple[str, dict[str, float]]],
    errored: list[tuple[str, str]],
) -> str:
    headers = [
        "symbol",
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
    for symbol, m in ranked:
        body.append(_metric_row(symbol, m))

    if insufficient:
        body.append(["─ insufficient (<%d trades) ─" % _MIN_TRADES_FOR_RANK] + [""] * (len(headers) - 1))
        for symbol, m in insufficient:
            body.append(_metric_row(symbol, m))

    if errored:
        body.append(["─ errored ─"] + [""] * (len(headers) - 1))
        for symbol, msg in errored:
            row = [symbol] + [""] * (len(headers) - 1)
            row[1] = msg[:30]
            body.append(row)

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


def _metric_row(symbol: str, m: dict[str, float]) -> list[str]:
    return [
        symbol,
        str(int(m.get("num_trades", 0))),
        _format_metric(m.get("win_rate", 0) * 100, decimals=1),
        _format_metric(m.get("avg_win"), decimals=2),
        _format_metric(m.get("avg_loss"), decimals=2),
        _format_metric(m.get("profit_factor"), decimals=2),
        _format_metric(m.get("total_return", 0) * 100, decimals=3),
        _format_metric(m.get("max_drawdown", 0) * 100, decimals=3),
        _format_metric(m.get("sharpe"), decimals=2),
    ]


def _profit_factor_for_sort(m: dict[str, float]) -> float:
    """Profit factor with None / inf handled for sort key.

    All-winners (no losses) returns inf — we map that to a large finite value
    so it sorts to the top. None / NaN / missing maps to -inf so it sorts
    last — that case should already be filtered into `insufficient`, but
    belt-and-braces.
    """
    pf = m.get("profit_factor")
    if pf is None:
        return float("-inf")
    try:
        pf_f = float(pf)
    except (TypeError, ValueError):
        return float("-inf")
    if pf_f != pf_f:  # NaN
        return float("-inf")
    return pf_f


def _write_summary_csv(out_dir: Path, all_rows: list[tuple[str, dict[str, float]]]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "summary.csv"
    fields = [
        "symbol",
        "num_trades",
        "win_rate",
        "avg_win",
        "avg_loss",
        "profit_factor",
        "total_return",
        "max_drawdown",
        "sharpe",
        "skipped_signals",
    ]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for symbol, m in all_rows:
            w.writerow([symbol] + [m.get(k, "") for k in fields[1:]])
    return path


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
    if not symbols:
        print("ERROR: no symbols given", file=sys.stderr)
        return 1

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = setup_logging(level=logging.INFO, run_id=f"screen-{run_id}")
    logger.info(
        "screen starting",
        extra={
            "symbols": symbols,
            "start": args.start,
            "end": args.end,
            "slippage_bps": args.slippage_bps,
            "commission": args.commission,
            "initial_cash": args.initial_cash,
            "log_path": str(log_path),
        },
    )

    cache = BarCache(Path(args.bars_dir))
    use_cache = not args.no_cache

    initial_cash = Decimal(args.initial_cash)
    costs = BacktestCosts(
        commission_per_share=Decimal(args.commission),
        slippage_bps=Decimal(args.slippage_bps),
    )
    # Backtest-only risk caps, kept generous so the engine doesn't reject
    # signals for cap reasons during a comparison run. Same values as
    # sweep_orb.py for apples-to-apples comparability across both screens.
    risk_params = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
    )

    cfg = load_config()
    print(f"\n== Symbol screen {args.start}..{args.end} ==")
    print(f"   symbols: {symbols}")
    print(f"   ORB cfg: {cfg}")
    print()

    all_rows: list[tuple[str, dict[str, float]]] = []
    errored: list[tuple[str, str]] = []
    for symbol in symbols:
        print(f"  fetching + backtesting {symbol} ...", flush=True)
        try:
            bars = _bars_for_range(
                symbol, start, end, cache=cache, use_cache=use_cache
            )
        except Exception as exc:
            errored.append((symbol, f"fetch error: {exc}"))
            continue
        if bars.empty:
            errored.append((symbol, "no bars in range"))
            continue
        bars = bars.copy()
        if "symbol" not in bars.columns:
            bars["symbol"] = symbol
        try:
            strategy = ORBStrategy(cfg)
            signals = strategy.generate_signals(bars)
            result = run_backtest(
                bars,
                signals,
                initial_cash=initial_cash,
                risk_params=risk_params,
                costs=costs,
                symbol=symbol,
            )
        except Exception as exc:
            errored.append((symbol, f"backtest error: {exc}"))
            logger.warning(
                "backtest error",
                extra={"symbol": symbol, "error": str(exc)},
            )
            continue
        metrics = dict(result.metrics)
        all_rows.append((symbol, metrics))
        logger.info(
            "symbol screened",
            extra={
                "symbol": symbol,
                "num_trades": int(metrics.get("num_trades", 0)),
                "win_rate": float(metrics.get("win_rate", 0)),
                "profit_factor": (
                    float(metrics["profit_factor"])
                    if metrics.get("profit_factor") is not None
                    else None
                ),
                "total_return": float(metrics.get("total_return", 0)),
                "max_drawdown": float(metrics.get("max_drawdown", 0)),
                "sharpe": (
                    float(metrics["sharpe"])
                    if metrics.get("sharpe") is not None
                    else None
                ),
            },
        )

    ranked = [(s, m) for s, m in all_rows if int(m.get("num_trades", 0)) >= _MIN_TRADES_FOR_RANK]
    insufficient = [
        (s, m) for s, m in all_rows if int(m.get("num_trades", 0)) < _MIN_TRADES_FOR_RANK
    ]
    ranked.sort(key=lambda row: _profit_factor_for_sort(row[1]), reverse=True)
    insufficient.sort(key=lambda row: int(row[1].get("num_trades", 0)), reverse=True)

    print()
    print(_render_screen_table(ranked, insufficient, errored))
    print()
    print("Sorted by profit_factor (descending) within the ranked group.")
    print("Reminder: historical results, not a forecast.")

    report_dir = Path(args.reports_dir) / f"screen-{run_id}"
    summary_path = _write_summary_csv(report_dir, all_rows)
    print(f"\nSummary written to: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
