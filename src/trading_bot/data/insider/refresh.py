"""Incremental Form 4 cache refresh.

The backfill machinery in ``backfill.py`` is resumable — it skips any
accession already present in the cache. ``refresh_cache_to_today`` is a
thin wrapper that calls ``run_backfill`` over a small trailing window
(default: 7 days) so the cache stays current without re-walking
12 months of quarterly indexes every time.

Used in two places:
  - ``scripts/run_paper.py`` calls it once at session startup (pre-flight),
    so the bot launches with a cache that includes every filing made
    between the last backfill and now.
  - The session loop calls it periodically (default every 15 min) so
    new filings published during the session are captured for the next
    trading day's signals.

Anti-lookahead reminder: a filing published during today's session
cannot trigger a same-day entry (see strategy/insider/strategy.py:21-25).
The periodic in-session refresh therefore affects tomorrow's signals,
not today's — but keeping the cache fresh during the day means tomorrow
morning's startup pre-flight does almost no work.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from .backfill import BackfillStats, run_backfill
from .cache import Form4Cache
from .client import EdgarClient
from .tickers import TickerMap

logger = logging.getLogger(__name__)


def refresh_cache_to_today(
    *,
    client: EdgarClient,
    cache: Form4Cache,
    ticker_map: TickerMap,
    lookback_days: int = 7,
    today: date | None = None,
) -> BackfillStats:
    """Bring the Form 4 cache up to ``today`` by backfilling the last
    ``lookback_days`` calendar days. Resumability inside ``run_backfill``
    makes repeated calls cheap.

    ``today`` defaults to ``date.today()``; explicit override is for tests.
    """
    end = today if today is not None else date.today()
    start = end - timedelta(days=lookback_days)
    logger.info(
        "insider cache refresh starting",
        extra={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    stats = run_backfill(
        client=client,
        cache=cache,
        ticker_map=ticker_map,
        start_date=start,
        end_date=end,
    )
    logger.info(
        "insider cache refresh done",
        extra={
            "quarters_scanned": stats.quarters_scanned,
            "index_entries_seen": stats.index_entries_seen,
            "filings_fetched": stats.filings_fetched,
            "filings_skipped_cached": stats.filings_skipped_cached,
            "filings_failed": stats.filings_failed,
            "rows_written": stats.rows_written,
        },
    )
    return stats
