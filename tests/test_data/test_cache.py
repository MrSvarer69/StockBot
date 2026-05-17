from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from trading_bot.data import BarCache


def _multi_day_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        pd.date_range("2026-01-02 14:30", periods=180, freq="1min", tz="UTC"),
        name="timestamp",
    )
    n = len(idx)
    return pd.DataFrame(
        {
            "open": [100.0 + i * 0.01 for i in range(n)],
            "high": [101.0 + i * 0.01 for i in range(n)],
            "low": [99.0 + i * 0.01 for i in range(n)],
            "close": [100.5 + i * 0.01 for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        },
        index=idx,
    )


def test_round_trip_single_day(tmp_path) -> None:
    cache = BarCache(tmp_path)
    df = _multi_day_frame()
    written = cache.write("SPY", df)
    assert len(written) == 1

    start = df.index[0].to_pydatetime()
    end = df.index[-1].to_pydatetime()
    out = cache.read("SPY", start, end)
    assert len(out) == len(df)
    assert (out.index == df.index).all()
    assert out["close"].iloc[0] == df["close"].iloc[0]


def test_multi_day_split(tmp_path) -> None:
    cache = BarCache(tmp_path)
    day1 = pd.date_range("2026-01-02 14:30", periods=60, freq="1min", tz="UTC")
    day2 = pd.date_range("2026-01-03 14:30", periods=60, freq="1min", tz="UTC")
    idx = day1.append(day2)
    idx.name = "timestamp"
    df = pd.DataFrame(
        {
            "open": [100.0] * len(idx),
            "high": [101.0] * len(idx),
            "low": [99.0] * len(idx),
            "close": [100.5] * len(idx),
            "volume": [1000] * len(idx),
        },
        index=idx,
    )
    written = cache.write("SPY", df)
    assert len(written) == 2

    dates = cache.available_dates("SPY")
    assert dates == [pd.Timestamp("2026-01-02").date(), pd.Timestamp("2026-01-03").date()]


def test_read_empty_when_no_cache(tmp_path) -> None:
    cache = BarCache(tmp_path)
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    end = datetime(2026, 1, 3, tzinfo=timezone.utc)
    out = cache.read("SPY", start, end)
    assert out.empty
    assert out.index.tz is not None


def test_read_filters_by_range(tmp_path) -> None:
    cache = BarCache(tmp_path)
    df = _multi_day_frame()
    cache.write("SPY", df)

    start = df.index[10].to_pydatetime()
    end = df.index[20].to_pydatetime()
    out = cache.read("SPY", start, end)
    assert len(out) == 11
    assert out.index[0] == df.index[10]
    assert out.index[-1] == df.index[20]


def test_symbol_case_insensitive_on_disk(tmp_path) -> None:
    cache = BarCache(tmp_path)
    df = _multi_day_frame()
    cache.write("spy", df)
    assert cache.available_dates("SPY") == cache.available_dates("spy")


class _StubFetcher:
    """Records calls and returns a pre-seeded frame on fetch."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls: list[tuple[str, datetime, datetime]] = []

    def fetch(self, symbol, start, end, timeframe="1Min"):
        self.calls.append((symbol, start, end))
        return self.frame


def test_read_or_fetch_uses_cache_when_coverage_complete(tmp_path):
    """If the cached range spans the requested range within tolerance, the
    fetcher must NOT be called (don't burn API on already-cached ranges)."""
    cache = BarCache(tmp_path)
    df = _multi_day_frame()  # 2026-01-02 14:30 → ~17:29
    cache.write("SPY", df)
    fetcher = _StubFetcher(pd.DataFrame())
    start = df.index[0].to_pydatetime()
    end = df.index[-1].to_pydatetime()
    out = cache.read_or_fetch("SPY", start, end, fetcher)
    assert not out.empty
    assert fetcher.calls == []


def test_read_or_fetch_fills_partial_cache(tmp_path):
    """Regression for 2026-05-14: cache holding only May 7-12 must NOT be
    treated as authoritative for a April-1 → May-13 request. The fetcher
    must be called and the merged result returned."""
    cache = BarCache(tmp_path)
    # Pre-seed a partial cache (only mid-window dates).
    partial_idx = pd.date_range(
        "2026-05-07 14:30", periods=60, freq="1min", tz="UTC"
    )
    partial = pd.DataFrame(
        {
            "open": [100.0] * len(partial_idx),
            "high": [101.0] * len(partial_idx),
            "low": [99.0] * len(partial_idx),
            "close": [100.5] * len(partial_idx),
            "volume": [1000] * len(partial_idx),
        },
        index=pd.DatetimeIndex(partial_idx, name="timestamp"),
    )
    cache.write("SPY", partial)
    # Fetcher returns a wider range covering both ends.
    wider_idx = pd.date_range(
        "2026-04-01 14:30", periods=60, freq="1min", tz="UTC"
    )
    wider = pd.DataFrame(
        {
            "open": [200.0] * len(wider_idx),
            "high": [201.0] * len(wider_idx),
            "low": [199.0] * len(wider_idx),
            "close": [200.5] * len(wider_idx),
            "volume": [2000] * len(wider_idx),
        },
        index=pd.DatetimeIndex(wider_idx, name="timestamp"),
    )
    fetcher = _StubFetcher(wider)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 13, 23, 59, tzinfo=timezone.utc)
    out = cache.read_or_fetch("SPY", start, end, fetcher)
    # Fetcher WAS called — the partial cache did not short-circuit.
    assert len(fetcher.calls) == 1
    # Result contains the merged range (both pre-seeded and fetched data).
    dates = sorted({ts.date() for ts in out.index})
    assert datetime(2026, 4, 1).date() in dates
    # The pre-seeded May 7 day is still present (fetcher didn't return it,
    # so cache.write only touched April 1; the original parquet survives).
    assert datetime(2026, 5, 7).date() in dates
    # April rows came from the fetcher (open=200) — overlay semantics:
    # newly-fetched data is what populates the previously-missing dates.
    apr_rows = out[out.index.normalize() == pd.Timestamp("2026-04-01", tz="UTC")]
    assert (apr_rows["open"] == 200.0).all()
    # Pre-seeded May 7 rows untouched (open=100).
    may_rows = out[out.index.normalize() == pd.Timestamp("2026-05-07", tz="UTC")]
    assert (may_rows["open"] == 100.0).all()


def test_read_or_fetch_skips_fetcher_when_cache_empty_and_fetcher_returns_empty(tmp_path):
    """A fetcher returning empty is allowed (e.g. weekends in the range).
    No write occurs; the empty cache stays empty."""
    cache = BarCache(tmp_path)
    fetcher = _StubFetcher(pd.DataFrame())
    start = datetime(2026, 1, 3, tzinfo=timezone.utc)  # Saturday
    end = datetime(2026, 1, 4, tzinfo=timezone.utc)  # Sunday
    out = cache.read_or_fetch("SPY", start, end, fetcher)
    assert out.empty
    assert len(fetcher.calls) == 1
    assert cache.available_dates("SPY") == []
