"""Backtest harness for Form 4 insider strategies (cluster_buy + csuite_conviction).

Filing-based signals do not fit the per-bar `run_backtest` shape (single
symbol, stop/take). This is a parallel harness:

- Load filings from data/insider/ within a date window.
- Run both strategies on the same filings DataFrame.
- Propagate filing_date from filings to each signal (anti-lookahead anchor).
- For the top-50 tickers by signal count, fetch daily bars from Alpaca.
- For each signal, enter at the next trading day open after filing_date and
  exit at the close of entry-day + N trading days for N in {5, 10, 20}.
- Costs: 1 bp main slippage, 3 bps stress, $0.005/share commission both sides.
- Sizing: $5,000 fixed notional per signal (no compounding, independent trades).
- Report PF / total return / win% / Pearson(strength, PnL) / quartile win-rate.

No live trading, no forecasts. Pure historical simulation.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from trading_bot.ops import setup_logging
from trading_bot.strategy.insider import (
    cluster_buy_signals,
    csuite_conviction_signals,
)

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

NOTIONAL_PER_SIGNAL = Decimal("5000")
COMMISSION_PER_SHARE = Decimal("0.005")
MAIN_SLIPPAGE_BPS = Decimal("1")
STRESS_SLIPPAGE_BPS = Decimal("3")
HOLDING_PERIODS = (5, 10, 20)
TOP_N_TICKERS = 50


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest for Form 4 insider strategies")
    p.add_argument("--start", default="2025-11-15")
    p.add_argument("--end", default="2026-05-13")
    p.add_argument("--insider-dir", default="data/insider")
    p.add_argument("--reports-dir", default="data/backtests")
    p.add_argument("--top-n", type=int, default=TOP_N_TICKERS)
    return p.parse_args(argv)


# ---------- Filings load + signal generation ---------------------------------


def load_filings(insider_dir: Path, start: date, end: date) -> pd.DataFrame:
    df = pd.read_parquet(insider_dir, engine="pyarrow")
    df["filing_date"] = pd.to_datetime(df["filing_date"])
    df["transaction_date"] = pd.to_datetime(df["transaction_date"])
    # We bound by FILING date: the signal cannot be acted on until filed.
    mask = (df["filing_date"] >= pd.Timestamp(start)) & (
        df["filing_date"] <= pd.Timestamp(end)
    )
    out = df[mask].reset_index(drop=True)
    return out


def attach_filing_dates(signals: pd.DataFrame, filings: pd.DataFrame) -> pd.DataFrame:
    """For each signal, attach the public-availability date = filing_date.

    cluster_buy:  use max(filing_date) across all (ticker, insider_cik, txn_date)
                  tuples whose insider_cik is in metadata['insider_ciks'] and
                  whose transaction_date is within [window_start, window_end].
                  This is when the *last* cluster member's filing went public.

    csuite:       single transaction; join on (ticker, insider_cik, signal_date)
                  where signal_date == transaction_date in the source.
    """
    if signals.empty:
        return signals.assign(filing_date=pd.NaT)

    out = signals.copy()
    filing_dates: list[pd.Timestamp | pd.NaT] = []

    # Index filings for fast lookup: (ticker, insider_cik, transaction_date) -> filing_date
    fdf = filings.copy()
    fdf["transaction_date"] = pd.to_datetime(fdf["transaction_date"]).dt.normalize()
    fdf["filing_date"] = pd.to_datetime(fdf["filing_date"]).dt.normalize()
    # For multiple rows with same key, take the max filing_date (latest disclosure).
    key_index = (
        fdf.groupby(["ticker", "insider_cik", "transaction_date"])["filing_date"]
        .max()
    )
    # Also a per-ticker, per-insider index for cluster window lookups.
    by_ticker_insider = fdf.set_index(["ticker", "insider_cik"]).sort_index()

    for _, row in out.iterrows():
        ticker = row["ticker"]
        meta = row["metadata"]
        strategy = row["strategy"]
        signal_date = pd.Timestamp(row["signal_date"]).normalize()

        if strategy == "insider.cluster_buy":
            window_start = pd.Timestamp(meta["window_start"]).normalize()
            window_end = pd.Timestamp(meta["window_end"]).normalize()
            ciks = meta["insider_ciks"]
            cluster_filing_dates: list[pd.Timestamp] = []
            for cik in ciks:
                try:
                    sub = by_ticker_insider.loc[(ticker, cik)]
                except KeyError:
                    continue
                if isinstance(sub, pd.Series):
                    sub = sub.to_frame().T
                m = (sub["transaction_date"] >= window_start) & (
                    sub["transaction_date"] <= window_end
                )
                fd = sub.loc[m, "filing_date"]
                if len(fd):
                    cluster_filing_dates.append(fd.max())
            if cluster_filing_dates:
                filing_dates.append(max(cluster_filing_dates))
            else:
                filing_dates.append(pd.NaT)
        elif strategy == "insider.csuite_conviction":
            cik = meta["insider_cik"]
            try:
                fd = key_index.loc[(ticker, cik, signal_date)]
                filing_dates.append(pd.Timestamp(fd))
            except KeyError:
                filing_dates.append(pd.NaT)
        else:
            filing_dates.append(pd.NaT)

    out["filing_date"] = filing_dates
    return out


# ---------- Daily bar fetch --------------------------------------------------


def fetch_daily_bars(
    symbols: list[str], start: datetime, end: datetime, api_key: str, api_secret: str
) -> dict[str, pd.DataFrame]:
    """Fetch daily bars for a list of symbols. Returns {symbol: DataFrame(UTC date-indexed)}.

    Single batched alpaca-py request, then split by symbol. IEX feed (free tier).
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(api_key, api_secret)
    out: dict[str, pd.DataFrame] = {}
    # Alpaca caps the symbols-per-request — batch in groups of 50 to be safe.
    BATCH = 50
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i : i + BATCH]
        req = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        raw = client.get_stock_bars(req).df
        if raw.empty:
            continue
        # raw is MultiIndex (symbol, timestamp). Split.
        for sym in batch:
            try:
                sub = raw.xs(sym, level="symbol")
            except KeyError:
                continue
            if sub.empty:
                continue
            sub = sub.copy()
            # Normalize timestamp to date (calendar trading days, UTC midnight).
            sub.index = pd.DatetimeIndex(sub.index)
            if sub.index.tz is None:
                sub.index = sub.index.tz_localize("UTC")
            else:
                sub.index = sub.index.tz_convert("UTC")
            # Keep only OHLCV-ish columns.
            keep = [c for c in ("open", "high", "low", "close", "volume") if c in sub.columns]
            sub = sub[keep].sort_index()
            out[sym] = sub
    return out


# ---------- Trade simulation -------------------------------------------------


def simulate_trade(
    bars: pd.DataFrame,
    entry_date: pd.Timestamp,  # earliest acceptable entry date (UTC date)
    holding_days: int,
    notional: Decimal,
    slippage_bps: Decimal,
    commission_per_share: Decimal,
) -> dict | None:
    """Simulate one long trade.

    Entry: open of the first bar with date >= entry_date.
    Exit: close of bar at index(entry_idx + holding_days). If we run out of bars,
          exit at the last available close (truncation handled by caller).

    Costs: commission both sides, slippage applied as (1 + bps/10000) on entry
    price and (1 - bps/10000) on exit price (long-only).
    """
    if bars.empty:
        return None

    # bars.index is UTC datetime; reduce to plain date for matching.
    bar_dates = pd.DatetimeIndex(bars.index).normalize()
    if bar_dates.tz is not None:
        bar_dates = bar_dates.tz_convert("UTC").tz_localize(None)
    target = pd.Timestamp(entry_date).normalize()
    if target.tz is not None:
        target = target.tz_convert("UTC").tz_localize(None)
    mask = np.asarray(bar_dates >= target)
    if not mask.any():
        return None
    entry_idx = int(np.argmax(mask))
    # mask is all-True after entry_idx; argmax returns the first True position.
    if entry_idx >= len(bars):
        return None
    exit_idx = entry_idx + holding_days
    truncated = False
    if exit_idx >= len(bars):
        exit_idx = len(bars) - 1
        truncated = True

    entry_row = bars.iloc[entry_idx]
    exit_row = bars.iloc[exit_idx]
    raw_entry_price = Decimal(str(entry_row["open"]))
    raw_exit_price = Decimal(str(exit_row["close"]))

    # Apply slippage: long fills worse on the open, sells worse on the close.
    slip = slippage_bps / Decimal("10000")
    eff_entry = raw_entry_price * (Decimal("1") + slip)
    eff_exit = raw_exit_price * (Decimal("1") - slip)

    if eff_entry <= 0:
        return None
    shares = int(notional / eff_entry)
    if shares < 1:
        return None

    gross_pnl = (eff_exit - eff_entry) * Decimal(shares)
    commission = commission_per_share * Decimal(shares) * Decimal("2")  # both sides
    net_pnl = gross_pnl - commission

    return {
        "entry_date": pd.Timestamp(bars.index[entry_idx]).normalize(),
        "exit_date": pd.Timestamp(bars.index[exit_idx]).normalize(),
        "entry_price": float(raw_entry_price),
        "exit_price": float(raw_exit_price),
        "eff_entry_price": float(eff_entry),
        "eff_exit_price": float(eff_exit),
        "shares": shares,
        "notional": float(eff_entry * Decimal(shares)),
        "gross_pnl": float(gross_pnl),
        "commission": float(commission),
        "net_pnl": float(net_pnl),
        "return_pct": float(net_pnl) / float(eff_entry * Decimal(shares)),
        "truncated": truncated,
    }


# ---------- Metrics ---------------------------------------------------------


def aggregate_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "n": 0,
            "win_rate": None,
            "profit_factor": None,
            "total_return_usd": 0.0,
            "avg_win": None,
            "avg_loss": None,
            "avg_pnl": None,
        }
    pnls = np.array([t["net_pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = float("inf")
    else:
        pf = None
    return {
        "n": int(len(trades)),
        "win_rate": float((pnls > 0).sum()) / len(pnls),
        "profit_factor": pf,
        "total_return_usd": float(pnls.sum()),
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "avg_pnl": float(pnls.mean()),
    }


def pearson_strength_pnl(trades: list[dict]) -> tuple[float | None, int]:
    if len(trades) < 5:
        return None, len(trades)
    s = np.array([t["strength"] for t in trades])
    p = np.array([t["net_pnl"] for t in trades])
    if s.std() == 0 or p.std() == 0:
        return None, len(trades)
    return float(np.corrcoef(s, p)[0, 1]), len(trades)


def quartile_winrate(trades: list[dict]) -> tuple[dict[str, float], dict[str, int]]:
    if len(trades) < 8:
        return {}, {}
    s = np.array([t["strength"] for t in trades])
    p = np.array([t["net_pnl"] for t in trades])
    qs = np.quantile(s, [0.25, 0.5, 0.75])
    out_wr: dict[str, float] = {}
    out_n: dict[str, int] = {}
    for q_lo, q_hi, label in [
        (-np.inf, qs[0], "Q1 (low)"),
        (qs[0], qs[1], "Q2"),
        (qs[1], qs[2], "Q3"),
        (qs[2], np.inf, "Q4 (high)"),
    ]:
        m = (s > q_lo) & (s <= q_hi)
        sl = p[m]
        if len(sl):
            out_wr[label] = float((sl > 0).sum()) / len(sl)
            out_n[label] = int(len(sl))
    return out_wr, out_n


# ---------- Reporting -------------------------------------------------------


def _fmt(v, decimals: int = 4) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "-"
    if v == float("inf"):
        return "inf"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_usd(v) -> str:
    if v is None:
        return "-"
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    setup_logging(level=logging.INFO, run_id=f"insider-bt-{run_id}")

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end_date < start_date:
        print("ERROR: --end before --start", file=sys.stderr)
        return 1

    insider_dir = Path(args.insider_dir)
    if not insider_dir.exists():
        print(f"ERROR: {insider_dir} not found", file=sys.stderr)
        return 1

    print(f"Loading filings from {insider_dir} ...", flush=True)
    filings = load_filings(insider_dir, start_date, end_date)
    print(f"  filings in window: {len(filings)}")
    # Defensive: drop rows with sentinel placeholder tickers (issuers with no
    # tradeable symbol -- bonds, private debt, etc.). The universal gates only
    # filter on .notna() so literal "NONE"/"N/A" leak through.
    bad_tickers = {"NONE", "N/A", "NA", "NULL", ""}
    before = len(filings)
    filings = filings[~filings["ticker"].isin(bad_tickers)].reset_index(drop=True)
    print(f"  dropped {before - len(filings)} rows with sentinel tickers")

    print("Generating cluster_buy signals ...", flush=True)
    cb_signals = cluster_buy_signals(filings, min_insiders=3, window_days=10)
    print(f"  cluster_buy signals: {len(cb_signals)}")
    print("Generating csuite_conviction signals ...", flush=True)
    cs_signals = csuite_conviction_signals(filings)
    print(f"  csuite_conviction signals: {len(cs_signals)}")

    print("Attaching filing_date (anti-lookahead anchor) ...", flush=True)
    cb_signals = attach_filing_dates(cb_signals, filings)
    cs_signals = attach_filing_dates(cs_signals, filings)

    # Drop signals with no filing_date resolvable (shouldn't happen but defensive).
    cb_signals = cb_signals.dropna(subset=["filing_date"]).reset_index(drop=True)
    cs_signals = cs_signals.dropna(subset=["filing_date"]).reset_index(drop=True)

    all_signals = pd.concat([cb_signals, cs_signals], ignore_index=True)
    print(f"  total signals with filing_date: {len(all_signals)}")
    print(
        f"  by strategy: {all_signals.groupby('strategy').size().to_dict()}"
    )

    # Top N tickers by signal count across both strategies.
    ticker_counts = all_signals.groupby("ticker").size().sort_values(ascending=False)
    top_tickers = ticker_counts.head(args.top_n).index.tolist()
    print(f"  top {args.top_n} tickers (by signal count): {top_tickers[:10]} ...")
    print(f"  total signals on top-{args.top_n}: "
          f"{all_signals[all_signals['ticker'].isin(top_tickers)].shape[0]}")

    # Restrict signals to top-N universe.
    signals = all_signals[all_signals["ticker"].isin(top_tickers)].reset_index(drop=True)

    # Fetch daily bars.
    api_key = os.environ.get("ALPACA_API_KEY", "")
    api_secret = os.environ.get("ALPACA_API_SECRET", "")
    if not api_key or not api_secret:
        print("ERROR: ALPACA_API_KEY/ALPACA_API_SECRET required", file=sys.stderr)
        return 1

    # We need bars from a few days before the earliest filing to ~25 trading
    # days after the latest filing (to cover the 20-day holding period).
    earliest_filing = signals["filing_date"].min()
    latest_filing = signals["filing_date"].max()
    fetch_start = (earliest_filing - timedelta(days=5)).to_pydatetime()
    fetch_end = (latest_filing + timedelta(days=45)).to_pydatetime()
    # Ensure UTC tz on the datetimes.
    if fetch_start.tzinfo is None:
        fetch_start = fetch_start.replace(tzinfo=UTC)
    if fetch_end.tzinfo is None:
        fetch_end = fetch_end.replace(tzinfo=UTC)
    print(f"Fetching daily bars for {len(top_tickers)} tickers "
          f"[{fetch_start.date()} -> {fetch_end.date()}] ...", flush=True)
    bars_by_symbol = fetch_daily_bars(
        top_tickers, fetch_start, fetch_end, api_key, api_secret
    )
    missing = [t for t in top_tickers if t not in bars_by_symbol]
    print(f"  fetched bars for {len(bars_by_symbol)} tickers; "
          f"missing {len(missing)}: {missing[:10]}")

    # Filter signals to tickers we actually have bars for.
    signals = signals[signals["ticker"].isin(bars_by_symbol)].reset_index(drop=True)
    print(f"  signals with bars available: {len(signals)}")

    # Simulate. For each (strategy x holding_period x slippage_level) cell,
    # produce a list of trades.
    cells: dict[tuple[str, int, str], list[dict]] = {}
    truncated_count = 0
    for _, row in signals.iterrows():
        ticker = row["ticker"]
        bars = bars_by_symbol[ticker]
        filing_date = pd.Timestamp(row["filing_date"]).normalize()
        # Anti-lookahead: cannot enter until the next trading day AFTER filing.
        # We hand simulate_trade an entry_date = filing_date + 1 calendar day,
        # and simulate_trade picks the first bar with date >= that.
        entry_anchor = filing_date + timedelta(days=1)
        strength = float(row["strength"])
        strategy = row["strategy"]
        for hp in HOLDING_PERIODS:
            for slip_label, slip in (
                ("1bp", MAIN_SLIPPAGE_BPS),
                ("3bps", STRESS_SLIPPAGE_BPS),
            ):
                t = simulate_trade(
                    bars=bars,
                    entry_date=entry_anchor,
                    holding_days=hp,
                    notional=NOTIONAL_PER_SIGNAL,
                    slippage_bps=slip,
                    commission_per_share=COMMISSION_PER_SHARE,
                )
                if t is None:
                    continue
                t["ticker"] = ticker
                t["strategy"] = strategy
                t["strength"] = strength
                t["filing_date"] = filing_date
                t["signal_date"] = pd.Timestamp(row["signal_date"]).normalize()
                t["holding_period"] = hp
                t["slippage_bps"] = float(slip)
                if t["truncated"]:
                    truncated_count += 1
                cells.setdefault((strategy, hp, slip_label), []).append(t)

    # ---------- Build report -----------
    report_dir = Path(args.reports_dir) / f"insider-{run_id}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # Per-cell aggregates (use 3 bps as the acceptance-bar slippage).
    aggregate_rows: list[dict] = []
    for strategy in sorted({c[0] for c in cells}):
        for hp in HOLDING_PERIODS:
            trades_1bp = cells.get((strategy, hp, "1bp"), [])
            trades_3bp = cells.get((strategy, hp, "3bps"), [])
            m1 = aggregate_metrics(trades_1bp)
            m3 = aggregate_metrics(trades_3bp)
            corr, n = pearson_strength_pnl(trades_3bp)
            aggregate_rows.append(
                {
                    "strategy": strategy,
                    "holding_period": hp,
                    "trades": m3["n"],
                    "win_rate": m3["win_rate"],
                    "pf_1bp": m1["profit_factor"],
                    "pf_3bps": m3["profit_factor"],
                    "total_return_usd_3bps": m3["total_return_usd"],
                    "avg_pnl_3bps": m3["avg_pnl"],
                    "pearson_strength_pnl": corr,
                    "pearson_n": n,
                }
            )

    # Write CSVs.
    def _write_csv(name: str, rows: list[dict], fields: list[str]) -> Path:
        path = report_dir / name
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    _write_csv(
        "aggregate_per_cell.csv",
        aggregate_rows,
        [
            "strategy",
            "holding_period",
            "trades",
            "win_rate",
            "pf_1bp",
            "pf_3bps",
            "total_return_usd_3bps",
            "avg_pnl_3bps",
            "pearson_strength_pnl",
            "pearson_n",
        ],
    )

    # Dump every trade (3 bps cell) for traceability.
    all_trades_rows: list[dict] = []
    for (strategy, hp, slip), tlist in cells.items():
        for t in tlist:
            all_trades_rows.append(
                {
                    "strategy": strategy,
                    "holding_period": hp,
                    "slippage": slip,
                    "ticker": t["ticker"],
                    "signal_date": t["signal_date"].date().isoformat(),
                    "filing_date": t["filing_date"].date().isoformat(),
                    "entry_date": t["entry_date"].date().isoformat(),
                    "exit_date": t["exit_date"].date().isoformat(),
                    "entry_price": t["entry_price"],
                    "exit_price": t["exit_price"],
                    "shares": t["shares"],
                    "gross_pnl": t["gross_pnl"],
                    "commission": t["commission"],
                    "net_pnl": t["net_pnl"],
                    "return_pct": t["return_pct"],
                    "strength": t["strength"],
                    "truncated": t["truncated"],
                }
            )
    _write_csv(
        "all_trades.csv",
        all_trades_rows,
        [
            "strategy",
            "holding_period",
            "slippage",
            "ticker",
            "signal_date",
            "filing_date",
            "entry_date",
            "exit_date",
            "entry_price",
            "exit_price",
            "shares",
            "gross_pnl",
            "commission",
            "net_pnl",
            "return_pct",
            "strength",
            "truncated",
        ],
    )

    # Determine acceptance verdict per cell.
    def cell_pass(r: dict) -> bool:
        pf_ok = (
            r["pf_3bps"] is not None
            and r["pf_3bps"] != float("inf")
            and r["pf_3bps"] > 1.0
        )
        ret_ok = (
            r["total_return_usd_3bps"] is not None and r["total_return_usd_3bps"] > 0
        )
        pear_ok = (
            r["pearson_strength_pnl"] is not None
            and r["pearson_strength_pnl"] > 0.10
            and r["pearson_n"] is not None
            and r["pearson_n"] >= 200
        )
        return pf_ok and ret_ok and pear_ok

    for r in aggregate_rows:
        r["passes_acceptance"] = cell_pass(r)

    passing = [r for r in aggregate_rows if r["passes_acceptance"]]

    # Pick "best" cell for the deeper diagnostics. If anything passes, the
    # highest PF passing cell wins. Otherwise the closest-to-passing.
    if passing:
        winning = max(passing, key=lambda r: (r["pf_3bps"] or 0, r["total_return_usd_3bps"] or 0))
    else:
        # Pick the cell with the best PF (treating None as 0) for diagnostics.
        winning = max(
            aggregate_rows,
            key=lambda r: ((r["pf_3bps"] or 0), r["total_return_usd_3bps"] or 0),
        )
    winning_trades = cells.get((winning["strategy"], winning["holding_period"], "3bps"), [])

    # Per-ticker top performers (winning cell).
    if winning_trades:
        per_ticker: dict[str, dict] = {}
        for t in winning_trades:
            d = per_ticker.setdefault(
                t["ticker"], {"ticker": t["ticker"], "trades": 0, "total_pnl": 0.0, "wins": 0}
            )
            d["trades"] += 1
            d["total_pnl"] += t["net_pnl"]
            if t["net_pnl"] > 0:
                d["wins"] += 1
        per_ticker_rows = sorted(
            per_ticker.values(), key=lambda d: abs(d["total_pnl"]), reverse=True
        )
        for d in per_ticker_rows:
            d["win_rate"] = d["wins"] / d["trades"]
        _write_csv(
            "winning_cell_per_ticker.csv",
            per_ticker_rows,
            ["ticker", "trades", "wins", "win_rate", "total_pnl"],
        )
    else:
        per_ticker_rows = []

    # Quartile win rate (winning cell).
    qwr, qn = quartile_winrate(winning_trades)

    # Filing-date breakdown -- our parquet carries filing_date as date-only
    # (Form 4 XML has no timestamp). Report business-day lag from
    # transaction_date -> filing_date as the closest meaningful proxy.
    lag_rows: list[dict] = []
    if not all_signals.empty:
        # Compute lag in business days using the raw filings (for each signal,
        # use the metadata transaction reference: window_end for cluster_buy,
        # signal_date for csuite).
        # For brevity we report on all_signals using filing_date - signal_date.
        all_signals["bday_lag"] = np.busday_count(
            all_signals["signal_date"].apply(lambda d: pd.Timestamp(d).date()).values.astype("datetime64[D]"),
            all_signals["filing_date"].apply(lambda d: pd.Timestamp(d).date()).values.astype("datetime64[D]"),
        )
        for lag_val, cnt in all_signals["bday_lag"].value_counts().sort_index().items():
            lag_rows.append({"bday_lag": int(lag_val), "count": int(cnt)})
    _write_csv("filing_lag_distribution.csv", lag_rows, ["bday_lag", "count"])

    # ----- Build markdown report -----
    lines: list[str] = []
    lines.append(f"# Insider strategy backtest -- run {run_id}")
    lines.append("")
    lines.append(f"- Window: {args.start} -> {args.end} (filing_date)")
    lines.append(f"- Filings in window: {len(filings)}")
    lines.append(f"- Strategies: insider.cluster_buy, insider.csuite_conviction")
    lines.append(
        f"- Universe: top {args.top_n} tickers by signal count "
        f"(across both strategies); bars actually fetched: {len(bars_by_symbol)}"
    )
    lines.append(
        f"- Holding periods tested (trading days): {list(HOLDING_PERIODS)}"
    )
    lines.append(
        f"- Costs: commission ${COMMISSION_PER_SHARE}/share both sides; "
        f"slippage {MAIN_SLIPPAGE_BPS} bp (main) / {STRESS_SLIPPAGE_BPS} bps (stress)"
    )
    lines.append(f"- Sizing: ${NOTIONAL_PER_SIGNAL} fixed notional per signal "
                 "(no compounding, independent trades)")
    lines.append(f"- Entry: next trading day open AFTER filing_date "
                 "(anti-lookahead anchor)")
    lines.append(f"- Truncated trades (insufficient bars after entry): {truncated_count}")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    if passing:
        lines.append(f"**PASS** -- {len(passing)} (strategy x hold) cells cleared the acceptance bar.")
    else:
        lines.append(
            "**FAIL** -- no (strategy x holding-period) cell cleared the acceptance bar."
        )
    lines.append("")
    lines.append("Acceptance bar:")
    lines.append("- PF > 1.0 at 3 bps slippage AND positive total return.")
    lines.append("- Pearson(strength, PnL) > 0.10 on N >= 200 paired trades.")
    lines.append("")

    # Aggregate table.
    lines.append("## 1. Headline aggregate (3 bps slippage)")
    lines.append("")
    lines.append(
        "| strategy | hold | trades | win% | PF (1bp) | PF (3bps) | "
        "tot_return | Pearson(strength,PnL) | N (paired) | verdict |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for r in aggregate_rows:
        lines.append(
            f"| {r['strategy'].split('.')[-1]} | {r['holding_period']} | "
            f"{r['trades']} | {_fmt_pct(r['win_rate'])} | "
            f"{_fmt(r['pf_1bp'], 2)} | {_fmt(r['pf_3bps'], 2)} | "
            f"{_fmt_usd(r['total_return_usd_3bps'])} | "
            f"{_fmt(r['pearson_strength_pnl'], 3)} | {r['pearson_n']} | "
            f"{'PASS' if r['passes_acceptance'] else 'FAIL'} |"
        )
    lines.append("")

    # Per-ticker top performers for winning cell.
    lines.append(
        f"## 2. Per-ticker top performers "
        f"({winning['strategy'].split('.')[-1]} x {winning['holding_period']}d)"
    )
    lines.append("")
    if per_ticker_rows:
        lines.append("| ticker | trades | wins | win% | total_pnl |")
        lines.append("| --- | --- | --- | --- | --- |")
        for d in per_ticker_rows[:10]:
            lines.append(
                f"| {d['ticker']} | {d['trades']} | {d['wins']} | "
                f"{_fmt_pct(d['win_rate'])} | {_fmt_usd(d['total_pnl'])} |"
            )
    else:
        lines.append("(no trades)")
    lines.append("")

    # Quartile win-rate.
    lines.append("## 3. Quartile win-rate (winning cell, by signal strength)")
    lines.append("")
    if qwr:
        lines.append("| quartile | win_rate | n |")
        lines.append("| --- | --- | --- |")
        for k, v in qwr.items():
            lines.append(f"| {k} | {_fmt_pct(v)} | {qn.get(k, 0)} |")
    else:
        lines.append("(not enough trades for quartile breakdown; need N >= 8)")
    lines.append("")

    # Filing lag distribution.
    lines.append("## 4. Filing-availability proxy (business-day lag, txn -> filing)")
    lines.append("")
    lines.append(
        "Note: the Form-4 parquet schema stores filing_date as date-only "
        "(SEC XML has no time-of-day). The time-of-day distribution requested "
        "cannot be derived from this data without joining EDGAR submission "
        "metadata (accession_no -> submission timestamp) which is out of scope. "
        "Reporting business-day lag from transaction_date to filing_date as the "
        "nearest meaningful breakdown."
    )
    lines.append("")
    if lag_rows:
        lines.append("| business_day_lag | signals |")
        lines.append("| --- | --- |")
        total_sigs = sum(r["count"] for r in lag_rows)
        for r in lag_rows:
            pct = r["count"] / total_sigs * 100 if total_sigs else 0
            lines.append(f"| {r['bday_lag']} | {r['count']} ({pct:.1f}%) |")
    else:
        lines.append("(no signals)")
    lines.append("")

    # Recommendation.
    lines.append("## 5. Recommendation")
    lines.append("")
    if passing:
        lines.append(
            f"PASS. Advance the **{winning['strategy'].split('.')[-1]}** strategy "
            f"with holding_period={winning['holding_period']} trading days. "
            "Live wiring: poll EDGAR every N minutes during the trading day, "
            "run the strategy on the rolling filings window, emit signals with "
            "`strength` as the picker score, enter at the next session open after "
            "the filing is observed, and exit at the close of the entry-day + N "
            "trading days. Position sizing remains $5,000 fixed notional per signal "
            "in the backtest config; live sizing must go through `trading_bot.risk`."
        )
    else:
        lines.append(
            "FAIL. No (strategy x holding-period) cell cleared the acceptance bar. "
            "This is the fifth strategy attempted; the user requested fail-fast "
            "judgement. Recommend stopping rather than iterating on a sixth strategy "
            "or relaxing the bar. The midday-coverage problem remains unsolved by "
            "Form 4 insider signals on this window/universe."
        )
    lines.append("")
    lines.append("## Reminder")
    lines.append("")
    lines.append(
        "Historical results computed from the cached/fetched bars + Form 4 cache. "
        "No forecasts."
    )

    (report_dir / "report.md").write_text("\n".join(lines))
    print(f"\nReport written to: {report_dir}/report.md")

    # Also print the headline table to stdout for the agent caller.
    print("\n=== HEADLINE AGGREGATE (3 bps) ===")
    for r in aggregate_rows:
        pf3 = r["pf_3bps"]
        pf3_s = f"{pf3:.2f}" if pf3 is not None and pf3 != float("inf") else str(pf3)
        print(
            f"  {r['strategy'].split('.')[-1]:>20s} | "
            f"hp={r['holding_period']:>2d}d | "
            f"N={r['trades']:>4d} | "
            f"win%={_fmt_pct(r['win_rate']):>7s} | "
            f"PF(3bps)={pf3_s:>6s} | "
            f"tot_ret={_fmt_usd(r['total_return_usd_3bps']):>10s} | "
            f"Pearson={_fmt(r['pearson_strength_pnl'], 3):>7s} | "
            f"verdict={'PASS' if r['passes_acceptance'] else 'FAIL'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
