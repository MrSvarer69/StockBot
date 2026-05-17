"""Two-dimensional ORB parameter tune across IS/OOS halves.

Usage:
  uv run python scripts/tune_orb.py \
      --symbols SNOW,MRVL,MU,NVDA,AMD,PLTR \
      --is-start 2025-11-15 --is-end 2026-02-15 \
      --oos-start 2026-02-16 --oos-end 2026-05-13 \
      --slippage-bps 3.0 \
      --min-range-values 0.3,0.5,0.7,1.0,1.5,2.0 \
      --atr-stop-values 1.0,1.5,2.0,2.5

For each symbol, for each (min_range_atr_multiplier, atr_stop_multiplier)
combo, runs the ORB backtest on BOTH the in-sample and out-of-sample halves
and reports both halves' profit factor along with a `floor_pf` column
(= min of the two). Sorted by floor_pf descending within each symbol.

Anti-overfit posture: see the report — a config that clears pf>1 in BOTH
halves on a SINGLE symbol is one lucky pair; a config that does it on
multiple symbols is more credible. Configs with <20 trades per half are
flagged as low-confidence.

Historical results only — never extrapolated, never a forecast.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.data import AlpacaBarFetcher, BarCache
from trading_bot.ops import setup_logging
from trading_bot.strategy import ORBStrategy, load_config
from trading_bot.strategy.orb.strategy import ORBConfig

logger = logging.getLogger(__name__)

# A config with < this many trades per half has too few samples for its pf
# to mean anything — we flag it as low-confidence in the report rather than
# excluding it outright (the reader can decide).
_LOW_CONFIDENCE_TRADE_THRESHOLD = 20


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="2-D ORB parameter tune across in-sample and out-of-sample halves."
    )
    p.add_argument("--symbols", required=True, help="Comma-separated symbol list")
    p.add_argument("--is-start", required=True, help="IS half start, YYYY-MM-DD inclusive")
    p.add_argument("--is-end", required=True, help="IS half end, YYYY-MM-DD inclusive")
    p.add_argument("--oos-start", required=True, help="OOS half start, YYYY-MM-DD inclusive")
    p.add_argument("--oos-end", required=True, help="OOS half end, YYYY-MM-DD inclusive")
    p.add_argument(
        "--min-range-values",
        default="0.3,0.5,0.7,1.0,1.5,2.0",
        help="Comma-separated min_range_atr_multiplier values",
    )
    p.add_argument(
        "--atr-stop-values",
        default="1.0,1.5,2.0,2.5",
        help="Comma-separated atr_stop_multiplier values",
    )
    p.add_argument("--initial-cash", default="100000")
    p.add_argument("--commission", default="0.005")
    p.add_argument("--slippage-bps", default="3.0")
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
        return cache.read(symbol, start, end)
    return cache.read_or_fetch(
        symbol,
        start,
        end,
        AlpacaBarFetcher(api_key=api_key, api_secret=api_secret),
    )


def _date_range_to_utc(start_str: str, end_str: str):
    start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
    if end_date < start_date:
        raise SystemExit(f"ERROR: --end {end_str} is before --start {start_str}")
    # One-day pad on each side so the first session has its prior-day ATR ready
    # and the last session is fully included.
    start = datetime.combine(start_date - timedelta(days=1), time(0, 0), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)
    return start, end


def _pf_value(m: dict) -> float | None:
    """Pull profit factor out of a metrics dict, normalizing inf/None/NaN."""
    pf = m.get("profit_factor")
    if pf is None:
        return None
    try:
        v = float(pf)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _floor_pf(pf_is: float | None, pf_oos: float | None) -> float:
    """Min of the two halves' pf. None on either side maps to -inf for sort."""
    if pf_is is None or pf_oos is None:
        return float("-inf")
    return min(pf_is, pf_oos)


def _fmt(v, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return "—"
    if f == float("inf"):
        return "inf"
    if f == float("-inf"):
        return "-inf"
    return f"{f:.{decimals}f}"


def _run_one(
    *,
    bars,
    symbol: str,
    cfg: ORBConfig,
    initial_cash: Decimal,
    risk_params: RiskParams,
    costs: BacktestCosts,
) -> dict:
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
    return dict(result.metrics)


def _render_symbol_table(symbol: str, rows: list[dict]) -> str:
    """Markdown table for a single symbol's full grid, sorted by floor_pf desc."""
    headers = [
        "min_rng",
        "atr_stop",
        "pf_IS",
        "pf_OOS",
        "floor_pf",
        "trades_IS",
        "trades_OOS",
        "dd_IS%",
        "dd_OOS%",
        "shp_IS",
        "shp_OOS",
        "flag",
    ]
    lines = [f"### {symbol}", ""]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        flag_parts = []
        if r["both_pass"]:
            flag_parts.append("PASS")
        if r["low_confidence"]:
            flag_parts.append("low-N")
        flag = ",".join(flag_parts) if flag_parts else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    _fmt(r["min_range"], 2),
                    _fmt(r["atr_stop"], 2),
                    _fmt(r["pf_is"], 2),
                    _fmt(r["pf_oos"], 2),
                    _fmt(r["floor_pf"], 2),
                    str(r["trades_is"]),
                    str(r["trades_oos"]),
                    _fmt(r["dd_is"] * 100, 2) if r["dd_is"] is not None else "—",
                    _fmt(r["dd_oos"] * 100, 2) if r["dd_oos"] is not None else "—",
                    _fmt(r["shp_is"], 2),
                    _fmt(r["shp_oos"], 2),
                    flag,
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()

    is_start, is_end = _date_range_to_utc(args.is_start, args.is_end)
    oos_start, oos_end = _date_range_to_utc(args.oos_start, args.oos_end)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("ERROR: no symbols given", file=sys.stderr)
        return 1

    try:
        min_range_vals = [float(v.strip()) for v in args.min_range_values.split(",") if v.strip()]
        atr_stop_vals = [float(v.strip()) for v in args.atr_stop_values.split(",") if v.strip()]
    except ValueError as exc:
        print(f"ERROR: bad numeric value in sweep grid: {exc}", file=sys.stderr)
        return 1

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = setup_logging(level=logging.WARNING, run_id=f"tune-{run_id}")
    logger.info(
        "tune starting",
        extra={
            "symbols": symbols,
            "is": (args.is_start, args.is_end),
            "oos": (args.oos_start, args.oos_end),
            "min_range_grid": min_range_vals,
            "atr_stop_grid": atr_stop_vals,
            "slippage_bps": args.slippage_bps,
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
    risk_params = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
    )

    base_cfg: ORBConfig = load_config()

    total_combos = len(symbols) * len(min_range_vals) * len(atr_stop_vals)
    print(f"\n== ORB 2-D tune ==")
    print(f"   symbols ({len(symbols)}): {symbols}")
    print(f"   IS:  {args.is_start} .. {args.is_end}")
    print(f"   OOS: {args.oos_start} .. {args.oos_end}")
    print(f"   min_range grid: {min_range_vals}")
    print(f"   atr_stop  grid: {atr_stop_vals}")
    print(f"   slippage_bps: {args.slippage_bps}")
    print(f"   total combos: {total_combos} (x 2 halves = {total_combos * 2} backtests)")
    print()

    # combo_key -> list of (symbol, row) — used after the loop to find configs
    # that work across multiple symbols.
    by_combo: dict[tuple[float, float], list[tuple[str, dict]]] = {}
    per_symbol_rows: dict[str, list[dict]] = {}
    errored: list[tuple[str, str]] = []

    for symbol in symbols:
        print(f"  {symbol}: loading bars ...", flush=True)
        try:
            bars_is = _bars_for_range(
                symbol, is_start, is_end, cache=cache, use_cache=use_cache
            )
            bars_oos = _bars_for_range(
                symbol, oos_start, oos_end, cache=cache, use_cache=use_cache
            )
        except Exception as exc:
            errored.append((symbol, f"fetch error: {exc}"))
            continue
        if bars_is.empty or bars_oos.empty:
            errored.append((symbol, "missing bars in one of the halves"))
            continue
        bars_is = bars_is.copy()
        bars_oos = bars_oos.copy()
        if "symbol" not in bars_is.columns:
            bars_is["symbol"] = symbol
        if "symbol" not in bars_oos.columns:
            bars_oos["symbol"] = symbol

        rows: list[dict] = []
        for mr in min_range_vals:
            for ast in atr_stop_vals:
                cfg = replace(
                    base_cfg,
                    min_range_atr_multiplier=mr,
                    atr_stop_multiplier=ast,
                )
                try:
                    m_is = _run_one(
                        bars=bars_is,
                        symbol=symbol,
                        cfg=cfg,
                        initial_cash=initial_cash,
                        risk_params=risk_params,
                        costs=costs,
                    )
                    m_oos = _run_one(
                        bars=bars_oos,
                        symbol=symbol,
                        cfg=cfg,
                        initial_cash=initial_cash,
                        risk_params=risk_params,
                        costs=costs,
                    )
                except Exception as exc:
                    errored.append((f"{symbol}@({mr},{ast})", f"backtest error: {exc}"))
                    continue

                pf_is = _pf_value(m_is)
                pf_oos = _pf_value(m_oos)
                floor = _floor_pf(pf_is, pf_oos)
                trades_is = int(m_is.get("num_trades", 0))
                trades_oos = int(m_oos.get("num_trades", 0))
                low_conf = (
                    trades_is < _LOW_CONFIDENCE_TRADE_THRESHOLD
                    or trades_oos < _LOW_CONFIDENCE_TRADE_THRESHOLD
                )
                both_pass = (
                    pf_is is not None
                    and pf_oos is not None
                    and pf_is > 1.0
                    and pf_oos > 1.0
                )
                row = {
                    "symbol": symbol,
                    "min_range": mr,
                    "atr_stop": ast,
                    "pf_is": pf_is,
                    "pf_oos": pf_oos,
                    "floor_pf": floor if floor != float("-inf") else None,
                    "trades_is": trades_is,
                    "trades_oos": trades_oos,
                    "dd_is": m_is.get("max_drawdown"),
                    "dd_oos": m_oos.get("max_drawdown"),
                    "shp_is": m_is.get("sharpe"),
                    "shp_oos": m_oos.get("sharpe"),
                    "ret_is": m_is.get("total_return"),
                    "ret_oos": m_oos.get("total_return"),
                    "winrate_is": m_is.get("win_rate"),
                    "winrate_oos": m_oos.get("win_rate"),
                    "both_pass": both_pass,
                    "low_confidence": low_conf,
                }
                rows.append(row)
                by_combo.setdefault((mr, ast), []).append((symbol, row))

        # Sort each symbol's grid by floor_pf descending; None floors sort last.
        rows.sort(
            key=lambda r: (r["floor_pf"] if r["floor_pf"] is not None else float("-inf")),
            reverse=True,
        )
        per_symbol_rows[symbol] = rows
        print(
            f"    done. {len(rows)} combos run. "
            f"{sum(1 for r in rows if r['both_pass'])} pass both halves. "
            f"{sum(1 for r in rows if r['both_pass'] and not r['low_confidence'])} pass both AND non-low-N."
        )

    # Find shared configs: combos where pf>1 in BOTH halves on multiple symbols
    shared_winners: list[tuple[tuple[float, float], list[tuple[str, dict]]]] = []
    for combo, entries in by_combo.items():
        passers = [(s, r) for s, r in entries if r["both_pass"]]
        if len(passers) >= 2:
            shared_winners.append((combo, passers))
    # Sort shared winners by number of symbols passing, then by mean floor_pf
    shared_winners.sort(
        key=lambda x: (
            len(x[1]),
            sum(r["floor_pf"] or 0 for _, r in x[1]) / max(len(x[1]), 1),
        ),
        reverse=True,
    )

    # === Output ===
    report_dir = Path(args.reports_dir) / f"tune-{run_id}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # Full grid CSV
    csv_path = report_dir / "grid.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "symbol",
                "min_range_atr_multiplier",
                "atr_stop_multiplier",
                "pf_is",
                "pf_oos",
                "floor_pf",
                "trades_is",
                "trades_oos",
                "max_dd_is",
                "max_dd_oos",
                "sharpe_is",
                "sharpe_oos",
                "total_return_is",
                "total_return_oos",
                "win_rate_is",
                "win_rate_oos",
                "both_pass",
                "low_confidence",
            ]
        )
        for symbol in symbols:
            for r in per_symbol_rows.get(symbol, []):
                w.writerow(
                    [
                        symbol,
                        r["min_range"],
                        r["atr_stop"],
                        "" if r["pf_is"] is None else r["pf_is"],
                        "" if r["pf_oos"] is None else r["pf_oos"],
                        "" if r["floor_pf"] is None else r["floor_pf"],
                        r["trades_is"],
                        r["trades_oos"],
                        r["dd_is"],
                        r["dd_oos"],
                        r["shp_is"],
                        r["shp_oos"],
                        r["ret_is"],
                        r["ret_oos"],
                        r["winrate_is"],
                        r["winrate_oos"],
                        r["both_pass"],
                        r["low_confidence"],
                    ]
                )

    # Markdown report
    md_path = report_dir / "report.md"
    lines: list[str] = []
    lines.append(f"# ORB 2-D Parameter Tune — IS vs OOS")
    lines.append("")
    lines.append(f"- Run ID: `tune-{run_id}`")
    lines.append(f"- Symbols: {', '.join(symbols)}")
    lines.append(f"- IS window:  {args.is_start} .. {args.is_end}")
    lines.append(f"- OOS window: {args.oos_start} .. {args.oos_end}")
    lines.append(f"- Slippage: {args.slippage_bps} bps, commission: {args.commission}/share")
    lines.append(f"- Grid: min_range_atr_multiplier = {min_range_vals}")
    lines.append(f"- Grid: atr_stop_multiplier = {atr_stop_vals}")
    lines.append(f"- Total combos per symbol: {len(min_range_vals) * len(atr_stop_vals)}")
    lines.append(f"- Total backtests: {total_combos * 2}")
    lines.append("")
    lines.append(
        "Historical results only. No forward-looking inference. Configs flagged "
        f"`low-N` had <{_LOW_CONFIDENCE_TRADE_THRESHOLD} trades in at least one half."
    )
    lines.append("")

    lines.append("## Headline")
    lines.append("")
    any_passers_per_symbol = {
        s: [r for r in per_symbol_rows.get(s, []) if r["both_pass"]] for s in symbols
    }
    n_symbols_with_any = sum(1 for v in any_passers_per_symbol.values() if v)
    n_symbols_with_strong = sum(
        1
        for v in any_passers_per_symbol.values()
        if any(not r["low_confidence"] for r in v)
    )
    lines.append(
        f"- **Symbols with ANY (min_range, atr_stop) clearing pf>1 on BOTH halves:** "
        f"{n_symbols_with_any} / {len(symbols)}"
    )
    lines.append(
        f"- **Symbols with at least one non-low-N pass:** "
        f"{n_symbols_with_strong} / {len(symbols)}"
    )
    lines.append(
        f"- **Shared configs (same combo passes on >=2 symbols):** {len(shared_winners)}"
    )
    lines.append("")

    lines.append("## Shared configs (cross-symbol robustness)")
    lines.append("")
    lines.append(
        "A single (min_range, atr_stop) pair that passes pf>1 in both halves on "
        "multiple symbols is much harder to fit by chance than per-symbol tunings."
    )
    lines.append("")
    if not shared_winners:
        lines.append("**No (min_range, atr_stop) combo cleared pf>1 in both halves on more than one symbol.**")
        lines.append("")
    else:
        lines.append("| min_rng | atr_stop | n_symbols | symbols | mean_floor_pf | min_floor_pf | any_low_N |")
        lines.append("|---|---|---|---|---|---|---|")
        for (mr, ast), passers in shared_winners:
            syms = ", ".join(s for s, _ in passers)
            floors = [r["floor_pf"] for _, r in passers if r["floor_pf"] is not None]
            mean_floor = sum(floors) / len(floors) if floors else None
            min_floor = min(floors) if floors else None
            any_low = any(r["low_confidence"] for _, r in passers)
            lines.append(
                f"| {_fmt(mr,2)} | {_fmt(ast,2)} | {len(passers)} | {syms} | "
                f"{_fmt(mean_floor,2)} | {_fmt(min_floor,2)} | {'yes' if any_low else 'no'} |"
            )
        lines.append("")

    lines.append("## Best-per-symbol (top 5)")
    lines.append("")
    lines.append(
        "Sorted by `floor_pf = min(pf_IS, pf_OOS)` — robustness, not best-case. "
        "If the top entry is dramatically better than the 2nd-5th, suspect overfit. "
        "If the plateau is broad, more credible."
    )
    lines.append("")
    lines.append(
        "| symbol | min_rng | atr_stop | pf_IS | pf_OOS | floor_pf | trades_IS | trades_OOS | flags |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for symbol in symbols:
        rows = per_symbol_rows.get(symbol, [])
        for r in rows[:5]:
            flag_parts = []
            if r["both_pass"]:
                flag_parts.append("PASS")
            if r["low_confidence"]:
                flag_parts.append("low-N")
            lines.append(
                f"| {symbol} | {_fmt(r['min_range'],2)} | {_fmt(r['atr_stop'],2)} | "
                f"{_fmt(r['pf_is'],2)} | {_fmt(r['pf_oos'],2)} | {_fmt(r['floor_pf'],2)} | "
                f"{r['trades_is']} | {r['trades_oos']} | {','.join(flag_parts) or '—'} |"
            )
    lines.append("")

    lines.append("## Full per-symbol grids")
    lines.append("")
    for symbol in symbols:
        rows = per_symbol_rows.get(symbol, [])
        lines.append(_render_symbol_table(symbol, rows))
        lines.append("")

    if errored:
        lines.append("## Errors")
        lines.append("")
        for label, msg in errored:
            lines.append(f"- `{label}`: {msg}")
        lines.append("")

    md_path.write_text("\n".join(lines))

    # Console summary
    print()
    print(f"== Summary ==")
    print(
        f"Symbols with ANY both-half passing config: {n_symbols_with_any} / {len(symbols)}"
    )
    print(
        f"Symbols with at least one non-low-N passing config: {n_symbols_with_strong} / {len(symbols)}"
    )
    print(f"Shared configs (pass on >=2 symbols): {len(shared_winners)}")
    if shared_winners:
        print("Top shared configs:")
        for (mr, ast), passers in shared_winners[:5]:
            syms = ", ".join(s for s, _ in passers)
            print(f"  ({mr}, {ast})  passes on {len(passers)}: {syms}")
    print()
    print(f"Grid CSV: {csv_path}")
    print(f"Report:   {md_path}")
    print()
    print("Reminder: historical results, not a forecast.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
