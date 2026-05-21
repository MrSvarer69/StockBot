"""Backfill SEC EDGAR Form 4 filings into the local Parquet cache.

Usage:
  uv run python scripts/backfill_form4.py --months 12
  uv run python scripts/backfill_form4.py --start 2025-05-17 --end 2026-05-17
  uv run python scripts/backfill_form4.py --months 1 --max-filings 200

Resumable: re-running skips already-cached filings. Raw XML is mirrored
under data/insider_raw/ so re-parsing never re-hits the network. The
Parquet root (data/insider/) is kept clean of non-parquet files so
pyarrow.dataset can scan it directly.

SEC requires a descriptive User-Agent. Set SEC_USER_AGENT in your .env:
    SEC_USER_AGENT="Your Name your.email@example.com"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

# Make src/ importable when running as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from trading_bot.data.insider import (  # noqa: E402  (after sys.path tweak)
    EdgarClient,
    Form4Cache,
    TickerMap,
    run_backfill,
)
from trading_bot.data.insider.index import (  # noqa: E402
    date_range_for_months_back,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    window = p.add_mutually_exclusive_group()
    window.add_argument(
        "--months",
        type=int,
        default=12,
        help="how many months back to backfill (default 12)",
    )
    window.add_argument(
        "--start",
        type=str,
        help="explicit start date YYYY-MM-DD (overrides --months)",
    )
    p.add_argument(
        "--end",
        type=str,
        help="explicit end date YYYY-MM-DD (defaults to today)",
    )
    p.add_argument(
        "--cache-root",
        type=Path,
        default=_REPO_ROOT / "data" / "insider",
        help="Parquet cache root (default data/insider)",
    )
    p.add_argument(
        "--raw-root",
        type=Path,
        default=_REPO_ROOT / "data" / "insider_raw",
        help="raw filing cache root (default data/insider_raw)",
    )
    p.add_argument(
        "--max-filings",
        type=int,
        default=None,
        help="cap on filings fetched this run (default: no cap)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="rows accumulated per Parquet write (default 500)",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def _resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    end_date = (
        datetime.strptime(args.end, "%Y-%m-%d").date()
        if args.end
        else date.today()
    )
    if args.start:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    else:
        start_date, end_date = date_range_for_months_back(args.months, today=end_date)
    return start_date, end_date


def main() -> int:
    load_dotenv()
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger("backfill_form4")

    start_date, end_date = _resolve_window(args)
    log.info("window: %s -> %s", start_date, end_date)

    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        log.warning(
            "SEC_USER_AGENT not set in environment; using fallback. "
            "Set it in .env per SEC's fair-access policy."
        )

    client = EdgarClient(cache_dir=args.raw_root)
    cache = Form4Cache(root=args.cache_root)
    ticker_map = TickerMap.from_client(client)
    log.info("ticker map size: %d", len(ticker_map))

    stats = run_backfill(
        client=client,
        cache=cache,
        ticker_map=ticker_map,
        start_date=start_date,
        end_date=end_date,
        batch_size=args.batch_size,
        max_filings=args.max_filings,
    )

    log.info("backfill complete: %s", stats)
    print("---- Form 4 backfill summary ----")
    print(f"  window:                 {start_date} -> {end_date}")
    print(f"  quarters scanned:       {stats.quarters_scanned}")
    print(f"  index entries in range: {stats.index_entries_seen}")
    print(f"  filings fetched:        {stats.filings_fetched}")
    print(f"  filings skipped (cached): {stats.filings_skipped_cached}")
    print(f"  filings failed:         {stats.filings_failed}")
    print(f"  rows written:           {stats.rows_written}")
    print(f"  parquet files written:  {stats.files_written}")
    print(f"  cache root:             {args.cache_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
