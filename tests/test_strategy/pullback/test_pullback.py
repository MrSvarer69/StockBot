"""Unit tests for PullbackStrategy.

The fixtures synthesize a single session with three controllable phases:
trending warmup -> pullback through EMA_fast -> reclaim. Tests pin each
component of the trigger contract in isolation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd

from trading_bot.strategy.pullback.strategy import (
    PullbackStrategy,
    load_config,
)

from .fixtures import make_session


def _cfg(**overrides):
    """Default to short EMA periods for unit tests so synthetic 390-bar
    fixtures clear warmup with room left for the pattern. Production config
    uses 50/200 EMAs; the strategy mechanic is the same."""
    base = load_config()
    overrides.setdefault("ema_fast_period", 20)
    overrides.setdefault("ema_slow_period", 50)
    overrides.setdefault("trend_slope_lookback", 10)
    overrides.setdefault("pullback_lookback_bars", 12)
    overrides.setdefault("min_pullback_bars", 3)
    overrides.setdefault("min_trend_strength", 0.5)
    overrides.setdefault("reclaim_confirm_bars", 2)
    return replace(base, **overrides)


def test_long_pullback_reclaim_fires_an_entry():
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_up_pullback_reclaim",
        pullback_start_bar=150,
        pullback_bars=6,
        pullback_depth=1.5,
    )
    cfg = _cfg(
        # Move the gate so the synthetic 09:30+150min = ~12:00 ET entry is
        # inside the window.
        earliest_entry_et="10:00",
        latest_entry_et="15:00",
    )
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    longs = sigs[sigs["side"] == "long"]
    assert len(longs) >= 1, (
        f"expected at least one long entry, got {len(longs)} "
        f"(all sigs: {sigs[['timestamp','side']].to_dict('records')})"
    )
    row = longs.iloc[0]
    assert pd.notna(row["stop_price"])
    assert pd.notna(row["take_price"])
    assert pd.notna(row["score"])
    # Bracket invariants for a long: stop below entry, take above. Entry
    # price is the bar close; we don't have that on the row but the stop
    # must be strictly less than the take for a long.
    assert row["stop_price"] < row["take_price"]


def test_short_pullback_reclaim_fires_an_entry():
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_down_pullback_reclaim",
        pullback_start_bar=150,
        pullback_bars=6,
        pullback_depth=1.5,
    )
    cfg = _cfg(
        earliest_entry_et="10:00",
        latest_entry_et="15:00",
    )
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    shorts = sigs[sigs["side"] == "short"]
    assert len(shorts) >= 1
    row = shorts.iloc[0]
    # Bracket invariants for a short: stop above take.
    assert row["stop_price"] > row["take_price"]


def test_no_pullback_no_entry():
    """A pure ramp without a dip through EMA_fast must not fire."""
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_up_no_pullback",
        pullback_start_bar=150,
    )
    cfg = _cfg(earliest_entry_et="10:00", latest_entry_et="15:00")
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    entries = sigs[sigs["side"].isin(["long", "short"])]
    assert entries.empty, (
        f"unexpected entries on pure trend: {entries.to_dict('records')}"
    )


def test_flat_tape_no_entry_no_trend():
    """A flat tape (no trend regime) must not produce entries even if
    closes cross the EMAs in noise."""
    bars = make_session(date(2026, 5, 5), phase="flat_no_trend")
    cfg = _cfg(earliest_entry_et="10:00", latest_entry_et="15:00")
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    entries = sigs[sigs["side"].isin(["long", "short"])]
    assert entries.empty


def test_entry_outside_time_window_is_suppressed():
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_up_pullback_reclaim",
        pullback_start_bar=20,  # would normally fire ~09:50 ET
        pullback_bars=6,
        pullback_depth=1.5,
    )
    cfg = _cfg(earliest_entry_et="11:00", latest_entry_et="14:00")
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    entries = sigs[sigs["side"].isin(["long", "short"])]
    # All entries must fall inside the gate.
    for ts in entries["timestamp"]:
        et = ts.tz_convert("America/New_York")
        assert pd.Timestamp("11:00").time() <= et.time() < pd.Timestamp("14:00").time(), (
            f"entry at {et} outside window"
        )


def test_flat_signal_emitted_at_session_end():
    bars = make_session(date(2026, 5, 5), phase="flat_no_trend")
    sigs = PullbackStrategy(_cfg()).generate_signals(bars)
    flats = sigs[sigs["side"] == "flat"]
    assert len(flats) == 1
    ts_et = flats.iloc[0]["timestamp"].tz_convert("America/New_York")
    assert ts_et.time() >= pd.Timestamp("15:55").time()


def test_cooldown_throttles_back_to_back_entries():
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_up_pullback_reclaim",
        pullback_start_bar=120,
        pullback_bars=4,
        pullback_depth=1.2,
        trend_slope=0.08,
    )
    cfg = _cfg(
        earliest_entry_et="10:00",
        latest_entry_et="15:30",
        cooldown_minutes=120,  # very long cooldown
    )
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    entries = sigs[sigs["side"].isin(["long", "short"])]
    # With a 120-min cooldown, only one entry can fire in a 5-hour window
    # even if the strategy's reclaim condition flickers across more bars.
    assert len(entries) <= 1


def test_warmup_skips_early_session_bars():
    """No entries should fire before the indicators are warm."""
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_up_pullback_reclaim",
        pullback_start_bar=5,  # before warmup ends
        pullback_bars=3,
        pullback_depth=2.0,
    )
    cfg = _cfg(
        earliest_entry_et="09:30",
        latest_entry_et="15:30",
        ema_slow_period=50,
        trend_slope_lookback=10,
    )
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    entries = sigs[sigs["side"].isin(["long", "short"])]
    # Any entry must come from a bar index >= warmup (50 + 10 = 60 here).
    for ts in entries["timestamp"]:
        bar_minute = ts.tz_convert("America/New_York").time()
        assert bar_minute >= pd.Timestamp("10:30").time()


def test_score_is_finite_and_positive_on_long():
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_up_pullback_reclaim",
        pullback_start_bar=150,
        pullback_bars=6,
        pullback_depth=1.5,
    )
    cfg = _cfg(earliest_entry_et="10:00", latest_entry_et="15:00")
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    longs = sigs[sigs["side"] == "long"]
    assert not longs.empty
    for score in longs["score"]:
        assert pd.notna(score)
        assert score > 0


def test_bars_must_be_tz_aware():
    import pytest

    bars = make_session(date(2026, 5, 5))
    bars_naive = bars.tz_localize(None)
    with pytest.raises(ValueError, match="tz-aware"):
        PullbackStrategy(load_config()).generate_signals(bars_naive)


def test_short_disabled_blocks_short_entries():
    bars = make_session(
        date(2026, 5, 5),
        phase="trend_down_pullback_reclaim",
        pullback_start_bar=150,
        pullback_bars=6,
        pullback_depth=1.5,
    )
    cfg = _cfg(
        earliest_entry_et="10:00",
        latest_entry_et="15:00",
        allow_short=False,
    )
    sigs = PullbackStrategy(cfg).generate_signals(bars)
    shorts = sigs[sigs["side"] == "short"]
    assert shorts.empty
