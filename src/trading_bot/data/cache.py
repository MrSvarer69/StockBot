"""Parquet-backed bar cache. One file per symbol per UTC session-date."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from .bars import REQUIRED_COLUMNS, validate_bars

if TYPE_CHECKING:
    from .fetch import BarFetcher

logger = logging.getLogger(__name__)


def _engine() -> str:
    try:
        import pyarrow  # noqa: F401

        return "pyarrow"
    except ImportError:
        return "fastparquet"


def _empty_single_symbol_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    df = pd.DataFrame(
        {
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="int64"),
        },
        index=index,
    )
    return df


class BarCache:
    """Read/write 1-minute bars partitioned as <root>/<symbol>/<YYYY-MM-DD>.parquet.

    Stored frames are single-symbol canonical BarsFrames.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _symbol_dir(self, symbol: str) -> Path:
        return self.root / symbol.upper()

    def _path(self, symbol: str, day: date) -> Path:
        return self._symbol_dir(symbol) / f"{day.isoformat()}.parquet"

    def write(self, symbol: str, df: pd.DataFrame) -> list[Path]:
        """Validate and write `df`, splitting into one file per UTC date.

        `df` must be a canonical single-symbol BarsFrame.
        """
        validate_bars(df, single_symbol=True)
        if df.empty:
            return []

        self._symbol_dir(symbol).mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        dates = df.index.normalize().unique()
        for day_ts in dates:
            day = day_ts.date()
            day_slice = df[df.index.normalize() == day_ts]
            if day_slice.empty:
                continue
            path = self._path(symbol, day)
            day_slice.to_parquet(path, engine=_engine())
            written.append(path)
        return written

    def read(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Return the canonical single-symbol frame covering the inclusive UTC range."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be tz-aware (UTC)")

        start_utc = pd.Timestamp(start).tz_convert("UTC")
        end_utc = pd.Timestamp(end).tz_convert("UTC")

        sym_dir = self._symbol_dir(symbol)
        if not sym_dir.exists():
            return _empty_single_symbol_frame()

        frames: list[pd.DataFrame] = []
        for day in self.available_dates(symbol):
            if day < start_utc.date() or day > end_utc.date():
                continue
            path = self._path(symbol, day)
            df = pd.read_parquet(path, engine=_engine())
            frames.append(df[list(REQUIRED_COLUMNS)])

        if not frames:
            return _empty_single_symbol_frame()

        out = pd.concat(frames).sort_index()
        out = out[(out.index >= start_utc) & (out.index <= end_utc)]
        if out.index.name != "timestamp":
            out.index.name = "timestamp"
        validate_bars(out, single_symbol=True)
        return out

    def read_or_fetch(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        fetcher: "BarFetcher",
        *,
        coverage_tolerance_days: int = 5,
    ) -> pd.DataFrame:
        """Read cached bars; fetch and merge if coverage is incomplete.

        Coverage check: if the cached date range does not span
        [start.date(), end.date()] within `coverage_tolerance_days` at each
        boundary, fetch the full range from `fetcher` and overlay onto the
        cache. Returns the merged frame for [start, end].

        Tolerance default 5 absorbs Thanksgiving Thu+early-close Fri+weekend
        (~4d) and Christmas/New Year stretches (4-5d). A trading-calendar
        lookup would be exact; 5 is the pragmatic non-dep approach.

        Pre-fix, the scripts' bar-loading helpers returned ANY cached data as
        authoritative — so a partial cache (e.g. 4 of 30 trading days) would
        silently produce an under-sampled backtest. This method is the central
        fix (now reached via `data.bars_for_range`).

        Intended for completed-session ranges. Calling with an `end` that
        lands inside an in-progress session can cause `self.write` to
        overwrite a complete cached day with a partial intraday slice.
        """
        cached = self.read(symbol, start, end)
        needs_fetch = cached.empty
        if not cached.empty:
            start_date = pd.Timestamp(start).tz_convert("UTC").date()
            end_date = pd.Timestamp(end).tz_convert("UTC").date()
            cached_min = cached.index.min().date()
            cached_max = cached.index.max().date()
            # prefix_gap is positive when cache starts AFTER the requested
            # start; negative when cache extends earlier than requested
            # (which is fine — kept signed so `> tolerance` catches only the
            # bad direction).
            prefix_gap = (cached_min - start_date).days
            suffix_gap = (end_date - cached_max).days
            needs_fetch = (
                prefix_gap > coverage_tolerance_days
                or suffix_gap > coverage_tolerance_days
            )
            if needs_fetch:
                logger.info(
                    "cache coverage incomplete; refetching",
                    extra={
                        "symbol": symbol,
                        "requested": [str(start_date), str(end_date)],
                        "cached": [str(cached_min), str(cached_max)],
                        "prefix_gap_days": prefix_gap,
                        "suffix_gap_days": suffix_gap,
                    },
                )
        if not needs_fetch:
            return cached
        fetched = fetcher.fetch(symbol, start, end)
        # Defense-in-depth: if we re-fetched because of incomplete coverage
        # and the fetcher returned an empty frame on a multi-day range, the
        # cache stays partial. Surface this so the operator notices instead
        # of running a backtest against silently-stale data.
        if (
            not cached.empty
            and fetched.empty
            and (pd.Timestamp(end).tz_convert("UTC").date()
                 - pd.Timestamp(start).tz_convert("UTC").date()).days >= 3
        ):
            logger.warning(
                "fetcher returned empty for a multi-day range with partial "
                "cache; returning stale cached data",
                extra={
                    "symbol": symbol,
                    "start": str(start),
                    "end": str(end),
                },
            )
        if not fetched.empty:
            self.write(symbol, fetched)
        return self.read(symbol, start, end)

    def available_dates(self, symbol: str) -> list[date]:
        sym_dir = self._symbol_dir(symbol)
        if not sym_dir.exists():
            return []
        dates: list[date] = []
        for entry in sym_dir.iterdir():
            if entry.suffix != ".parquet":
                continue
            try:
                dates.append(date.fromisoformat(entry.stem))
            except ValueError:
                continue
        return sorted(dates)
