"""Prior-session bar loading from the local Parquet cache.

The pullback strategy needs ~200 bars of warm-up for its EMA_slow; at 1-min
bars that is ~3.3 hours into the regular session. To let the strategy fire
near the open, we pre-warm its indicator buffer with bars from one or more
PRIOR trading sessions stored in the cache.

The cache layout (see `BarCache`) is `<root>/<symbol>/<YYYY-MM-DD>.parquet`,
one file per UTC date. A US regular session (13:30 - 20:00 UTC) lives
entirely inside one UTC date, so each parquet file is treated as one prior
session here.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pandas as pd

from .bars import REQUIRED_COLUMNS, validate_bars
from .cache import BarCache, _empty_single_symbol_frame

logger = logging.getLogger(__name__)

# Cap how far back we scan when the prior session is sparse (holiday short
# session, missing cache day). 10 calendar days comfortably covers a
# Thanksgiving / Christmas stretch without scanning unbounded history.
_DEFAULT_MAX_LOOKBACK_DAYS = 10


def load_prior_session_bars(
    symbol: str,
    today: pd.Timestamp,
    min_bars: int = 200,
    *,
    cache: BarCache | None = None,
    bars_dir: Path | str = "data/bars",
    max_lookback_days: int = _DEFAULT_MAX_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Return canonical 1-min bars from the most recent prior session(s).

    Walks backwards from `today.date() - 1 day`, accumulating cached sessions
    until at least `min_bars` rows are collected or the lookback cap is
    reached. `today` is interpreted as a UTC date; the helper returns bars
    strictly before midnight UTC of that date.

    If the cache is empty or no prior session is reachable within
    `max_lookback_days` calendar days, an empty canonical single-symbol
    BarsFrame is returned. The helper never raises on missing data and never
    touches the network -- it is read-only over the local Parquet cache.

    Returned frame is concatenated, sorted ascending, and validated against
    the canonical single-symbol BarsFrame contract.
    """
    if cache is None:
        cache = BarCache(Path(bars_dir))

    today_ts = pd.Timestamp(today)
    if today_ts.tzinfo is None:
        today_ts = today_ts.tz_localize("UTC")
    else:
        today_ts = today_ts.tz_convert("UTC")
    today_date = today_ts.date()

    available = set(cache.available_dates(symbol))
    if not available:
        logger.debug(
            "prior-session cache miss",
            extra={"symbol": symbol, "today": str(today_date), "reason": "no cache"},
        )
        return _empty_single_symbol_frame()

    frames: list[pd.DataFrame] = []
    total = 0
    # Walk calendar days backwards; skip days with no cache file (weekend,
    # holiday, gap). Stop once we have enough bars OR we hit the cap.
    cursor = today_date - timedelta(days=1)
    stop_at = today_date - timedelta(days=max_lookback_days)
    while cursor >= stop_at and total < min_bars:
        if cursor in available:
            path = cache._path(symbol, cursor)
            try:
                df = pd.read_parquet(path)
            except Exception:
                logger.debug(
                    "prior-session parquet read failed",
                    extra={"symbol": symbol, "day": str(cursor)},
                )
                cursor = cursor - timedelta(days=1)
                continue
            if not df.empty:
                frames.append(df[list(REQUIRED_COLUMNS)])
                total += len(df)
        cursor = cursor - timedelta(days=1)

    if not frames:
        return _empty_single_symbol_frame()

    # Frames were collected newest-first; concat then sort by index so the
    # output is monotonically increasing, matching the canonical contract.
    out = pd.concat(frames).sort_index()
    # Defensive: a duplicated bar across two day-files would violate the
    # canonical contract. Keep the first occurrence; cache.write already
    # partitions by UTC date so collisions should not happen in practice.
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="first")]
    if out.index.name != "timestamp":
        out.index.name = "timestamp"
    validate_bars(out, single_symbol=True)
    return out


__all__ = ["load_prior_session_bars"]
