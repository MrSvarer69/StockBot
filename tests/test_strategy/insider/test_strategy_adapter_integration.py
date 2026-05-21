"""Integration test for ``InsiderStrategy`` against the real parquet cache.

This test is marked ``integration`` and skips on a clean clone where
``data/insider/`` is absent or empty. It exists to:

  1. Sanity-floor the cached Form 4 signal count (~635 known signals across
     ~257 tickers when this test was written — we floor at 100 to leave
     headroom for partial reseeds while still catching catastrophic loss).
  2. Verify the sentinel-ticker guard in ``apply_universal_gates`` actually
     scrubs the on-disk cache: no signal in the loaded index has a
     ``NONE`` / ``N/A`` / empty-string ticker.
  3. Exercise the end-to-end ``generate_signals(bars)`` path against
     synthetic bars constructed around a real loaded entry date, confirming
     the SignalsFrame contract is respected.

The construction-time parquet read is the only I/O — once the adapter is
built, ``generate_signals`` itself remains pure over (bars, in-memory state).
"""

from __future__ import annotations

from datetime import date, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_bot.strategy.base import empty_signals_frame
from trading_bot.strategy.insider import InsiderStrategy, load_config


ET = ZoneInfo("America/New_York")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_INSIDER_CACHE = _REPO_ROOT / "data" / "insider"


def _cache_is_populated() -> bool:
    """Return True iff the parquet cache exists and contains at least one
    parquet file. Used to skip cleanly on a clone with no data backfill."""
    if not _INSIDER_CACHE.exists():
        return False
    return any(_INSIDER_CACHE.rglob("*.parquet"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _cache_is_populated(),
        reason="parquet cache not present",
    ),
]


def _make_synthetic_bars_for_date(
    symbol: str, session_date: date, minutes: int = 390
) -> pd.DataFrame:
    """Build one trading day of synthetic 1-min bars for ``symbol`` on
    ``session_date``. UTC index, ET-aligned 09:30 open.

    Why these defaults:
      - 390 minutes = a full regular-session day (09:30-16:00 ET).
      - constant-drift OHLC pattern gives ATR a non-zero warmup so the
        entry-bar check (``atr_bar > 0``) passes.
    """
    start = pd.Timestamp.combine(session_date, dtime(9, 30)).tz_localize(ET).tz_convert("UTC")
    idx = pd.date_range(start, periods=minutes, freq="1min", tz="UTC")
    closes = [100.0 + 0.1 * (i % 5) for i in range(minutes)]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.2 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.2 for o, c in zip(opens, closes)]
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * minutes,
            "symbol": [symbol] * minutes,
        },
        index=idx,
    )


def test_adapter_loads_real_cache_and_produces_signals() -> None:
    """End-to-end sanity check on a populated cache.

    Asserts in order:
      - the adapter built a non-empty entry-date index
      - total loaded signal count clears the 100-signal sanity floor
      - no loaded signal carries a sentinel ticker (gates fix verification)
      - generate_signals over synthetic bars on a known entry date yields
        a non-empty SignalsFrame with the canonical empty_signals_frame()
        column contract
    """
    cfg = load_config()
    ins = InsiderStrategy(cfg)

    # --- (1) index non-empty + (2) sanity-floor signal count -------------
    n_dates = len(ins._signals_by_entry_date)
    assert n_dates > 0, "expected at least one entry date in loaded index"
    n_signals = sum(len(v) for v in ins._signals_by_entry_date.values())
    assert n_signals > 100, (
        f"loaded only {n_signals} signals; expected >100 from cached cache"
    )

    # --- (3) sentinel-ticker scrub ---------------------------------------
    # Why: the gates layer normalizes case and whitespace, so we replicate
    # that normalization here when checking the loaded index.
    sentinels = {"NONE", "N/A", "NA", "NULL", ""}
    for sigs in ins._signals_by_entry_date.values():
        for sig in sigs:
            tk = (sig["ticker"] or "").strip().upper()
            assert tk not in sentinels, (
                f"sentinel ticker {sig['ticker']!r} leaked through gates"
            )

    # --- (4) generate_signals on a synthetic bars frame ------------------
    # Pick the ticker with the most signals so we maximize the chance of a
    # hit on whichever entry date we test against.
    ticker_counts: dict[str, int] = {}
    for sigs in ins._signals_by_entry_date.values():
        for sig in sigs:
            ticker_counts[sig["ticker"]] = ticker_counts.get(sig["ticker"], 0) + 1
    top_ticker = max(ticker_counts, key=ticker_counts.get)

    # Find an entry date for which the top_ticker has a signal. Why we need
    # to scan: a ticker can have many signals across many dates; we want
    # the bars frame to cover one of the dates where it actually fires.
    target_entry_date: date | None = None
    for entry_date, sigs in ins._signals_by_entry_date.items():
        if any(s["ticker"] == top_ticker for s in sigs):
            target_entry_date = entry_date
            break
    assert target_entry_date is not None

    # Why: effective_entry_date is one trading day *after* max_filing_date,
    # so the bars window must include target_entry_date itself. We build a
    # 3-day window centered on target_entry_date for ATR warmup headroom.
    sessions = []
    cursor = target_entry_date
    # Step back two weekdays for ATR warmup, then forward one for headroom.
    weekdays_back = 0
    while weekdays_back < 2:
        cursor = pd.Timestamp(cursor) - pd.Timedelta(days=1)
        if cursor.weekday() < 5:
            sessions.insert(0, cursor.date())
            weekdays_back += 1
        else:
            cursor = cursor.date() if hasattr(cursor, "date") else cursor
    sessions.append(target_entry_date)
    # +1 weekday after entry to give the forced-flat resolver something to
    # land on if holding_days > 0.
    cursor = pd.Timestamp(target_entry_date)
    while True:
        cursor = cursor + pd.Timedelta(days=1)
        if cursor.weekday() < 5:
            sessions.append(cursor.date())
            break

    bars = pd.concat(
        [_make_synthetic_bars_for_date(top_ticker, d) for d in sessions],
        axis=0,
    ).sort_index()

    signals = ins.generate_signals(bars)
    assert not signals.empty, (
        f"expected at least one signal row for {top_ticker} on {target_entry_date}"
    )

    # Column contract — every column from empty_signals_frame() must exist
    # on the produced frame, in the same set (order matched separately by
    # the adapter's own column-select at the end of generate_signals).
    expected_cols = set(empty_signals_frame().columns)
    assert set(signals.columns) == expected_cols

    # And at least one row must be a long entry on the target ticker.
    entries = signals[(signals["side"] == "long") & (signals["symbol"] == top_ticker)]
    assert not entries.empty
