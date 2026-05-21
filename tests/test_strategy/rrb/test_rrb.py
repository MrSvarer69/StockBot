"""Unit tests for RRBStrategy (Rolling Range Breakout).

Pin behavior on small synthetic fixtures. No I/O, no broker, no network --
the strategy is a pure function and these tests stay that way too.

The load-bearing test of this redesign is
``test_score_increases_monotonically_with_breakout_depth``: the picker
will rank cross-strategy signals by the score column, and that ranking
only does work if the score has a mechanical relationship to expected
PnL. We assert the monotonic ordering directly on synthetic inputs where
the answer is known.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from trading_bot.strategy.rrb.strategy import RRBStrategy, load_config

from .fixtures import make_session


def _cfg(**overrides):
    """Default config with optional overrides. Tests that need to bypass
    the warm-up gate or shorten the lookback set those explicitly."""
    cfg = load_config()
    return replace(cfg, **overrides)


# ---------------------------------------------------------------------------
# Signal rule firing
# ---------------------------------------------------------------------------


def test_breakout_up_produces_a_long_entry():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    strat = RRBStrategy(_cfg())
    signals = strat.generate_signals(bars)

    longs = signals[signals["side"] == "long"]
    assert len(longs) >= 1, signals
    entry = longs.iloc[0]
    assert entry["stop_price"] < entry["take_price"]
    assert pd.notna(entry["or_atr_ratio"]) and entry["or_atr_ratio"] > 0


def test_breakdown_down_produces_a_short_entry():
    bars = make_session(date(2026, 1, 5), pattern="breakdown_down")
    strat = RRBStrategy(_cfg(allow_short=True))
    signals = strat.generate_signals(bars)

    shorts = signals[signals["side"] == "short"]
    assert len(shorts) >= 1, signals
    entry = shorts.iloc[0]
    assert entry["stop_price"] > entry["take_price"]


def test_first_long_entry_is_at_or_after_the_breakout_bar():
    """First long must coincide with -- or follow -- the bar at which the
    deterministic upside move was injected (not before)."""
    breakout_start_bar = 75
    bars = make_session(
        date(2026, 1, 5),
        pattern="breakout_up",
        breakout_start_bar=breakout_start_bar,
    )
    strat = RRBStrategy(_cfg())
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    assert len(longs) >= 1
    first_long_ts = longs.iloc[0]["timestamp"]
    breakout_ts = bars.index[breakout_start_bar]
    assert first_long_ts >= breakout_ts


# ---------------------------------------------------------------------------
# No-fire conditions
# ---------------------------------------------------------------------------


def test_flat_tape_produces_no_entries():
    bars = make_session(date(2026, 1, 5), pattern="flat")
    strat = RRBStrategy(_cfg())
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0
    flats = signals[signals["side"] == "flat"]
    assert len(flats) == 1


def test_shallow_poke_below_min_strength_does_not_fire():
    """A breakout that closes less than ``min_breakout_strength`` ATRs
    through the rolling high is in the noise band and must not fire."""
    # ATR on 1-min bars in the fixture is ~0.1 (bar high-low ~0.1 on a
    # symmetric oscillation). A breakout_size of 0.01 keeps the close
    # essentially inside the band envelope; the strategy should not fire.
    bars = make_session(
        date(2026, 1, 5),
        pattern="breakout_up",
        breakout_size=0.01,
    )
    strat = RRBStrategy(_cfg(min_breakout_strength=0.25))
    signals = strat.generate_signals(bars)
    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0


def test_allow_short_false_blocks_short_entry():
    bars = make_session(date(2026, 1, 5), pattern="breakdown_down")
    strat = RRBStrategy(_cfg(allow_short=False))
    signals = strat.generate_signals(bars)
    assert (signals["side"] != "short").all()


def test_warmup_window_suppresses_signals_before_lookback_full():
    """With require_full_window=True and lookback_minutes=60, the first
    60 bars of the session must not produce any entry signals -- the
    rolling reference is not yet defined."""
    # Inject a breakout at bar 20 (well inside the warm-up window).
    bars = make_session(
        date(2026, 1, 5),
        pattern="breakout_up",
        breakout_start_bar=20,
        warmup_bars=60,
    )
    strat = RRBStrategy(_cfg(lookback_minutes=60, require_full_window=True))
    signals = strat.generate_signals(bars)
    entries = signals[signals["side"].isin(["long", "short"])]
    if len(entries) > 0:
        # If anything fires, it must be at or after bar 60.
        bar_60_ts = bars.index[60]
        assert entries.iloc[0]["timestamp"] >= bar_60_ts


def test_latest_entry_et_suppresses_late_day_entries():
    """A breakout injected after the latest_entry_et cutoff must not produce
    a new entry (flat signal is unaffected)."""
    # 09:30 ET + 330 minutes = 15:00 ET, i.e. the cutoff. Push the breakout
    # to bar 331 so it lands strictly after the cutoff.
    bars = make_session(
        date(2026, 1, 5),
        pattern="breakout_up",
        breakout_start_bar=331,
    )
    strat = RRBStrategy(_cfg(latest_entry_et="15:00"))
    signals = strat.generate_signals(bars)
    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0
    flats = signals[signals["side"] == "flat"]
    assert len(flats) == 1


def test_cooldown_prevents_back_to_back_long_entries():
    """A sustained breakout would otherwise fire a new long on every bar
    that keeps making rolling highs; cooldown enforces one entry per
    cooldown_minutes window."""
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    strat = RRBStrategy(_cfg(cooldown_minutes=120))
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    # The fixture extends well past 120 bars of breakout; with cooldown=120,
    # we expect at most a couple of long entries, not one per bar.
    assert len(longs) <= 3
    if len(longs) >= 2:
        gap = (longs.iloc[1]["timestamp"] - longs.iloc[0]["timestamp"]).total_seconds() / 60
        assert gap >= 120


# ---------------------------------------------------------------------------
# Stop / take computation -- asymmetric by construction
# ---------------------------------------------------------------------------


def test_long_stop_and_take_use_atr_and_r_multiple():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    strat = RRBStrategy(_cfg(atr_stop_multiplier=1.0, take_r_multiple=2.0))
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    assert len(longs) >= 1
    entry = longs.iloc[0]
    close = float(bars.loc[entry["timestamp"], "close"])
    risk = close - entry["stop_price"]
    reward = entry["take_price"] - close
    assert risk > 0
    assert reward == pytest.approx(2.0 * risk, rel=1e-9)


def test_short_stop_and_take_mirror_long():
    bars = make_session(date(2026, 1, 5), pattern="breakdown_down")
    strat = RRBStrategy(_cfg(atr_stop_multiplier=1.0, take_r_multiple=2.0))
    signals = strat.generate_signals(bars)
    shorts = signals[signals["side"] == "short"]
    assert len(shorts) >= 1
    entry = shorts.iloc[0]
    close = float(bars.loc[entry["timestamp"], "close"])
    risk = entry["stop_price"] - close
    reward = close - entry["take_price"]
    assert risk > 0
    assert reward == pytest.approx(2.0 * risk, rel=1e-9)


def test_default_config_is_asymmetric_with_take_greater_than_stop_distance():
    """Default config must give a >1 reward/risk ratio, not symmetric."""
    cfg = load_config()
    assert cfg.take_r_multiple > 1.0, (
        "RRB default must provide asymmetric reward; "
        f"take_r_multiple={cfg.take_r_multiple}"
    )


def test_both_stop_and_take_set_on_every_entry():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    strat = RRBStrategy(_cfg())
    signals = strat.generate_signals(bars)
    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) >= 1
    assert entries["stop_price"].notna().all()
    assert entries["take_price"].notna().all()
    flats = signals[signals["side"] == "flat"]
    if len(flats):
        assert flats["stop_price"].isna().all()
        assert flats["take_price"].isna().all()


# ---------------------------------------------------------------------------
# Pure-function behavior + schema
# ---------------------------------------------------------------------------


def test_strategy_does_not_mutate_input_bars():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    snapshot = bars.copy(deep=True)
    strat = RRBStrategy(_cfg())
    strat.generate_signals(bars)
    pd.testing.assert_frame_equal(bars, snapshot)


def test_same_input_yields_identical_output():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    strat = RRBStrategy(_cfg())
    a = strat.generate_signals(bars)
    b = strat.generate_signals(bars)
    pd.testing.assert_frame_equal(
        a.reset_index(drop=True), b.reset_index(drop=True)
    )


def test_empty_bars_returns_empty_signals_frame():
    empty = pd.DataFrame(
        {
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
            "symbol": [],
        }
    )
    empty.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    strat = RRBStrategy(_cfg())
    out = strat.generate_signals(empty)
    assert len(out) == 0
    for col in (
        "timestamp", "symbol", "side", "target_size_pct",
        "stop_price", "take_price", "or_atr_ratio",
    ):
        assert col in out.columns


def test_missing_symbol_raises():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up").drop(columns=["symbol"])
    strat = RRBStrategy(_cfg())
    with pytest.raises(ValueError, match="symbol"):
        strat.generate_signals(bars)


def test_naive_index_raises():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    bars.index = bars.index.tz_convert("UTC").tz_localize(None)
    strat = RRBStrategy(_cfg())
    with pytest.raises(ValueError, match="tz-aware"):
        strat.generate_signals(bars)


# ---------------------------------------------------------------------------
# Score: load-bearing monotonic test
# ---------------------------------------------------------------------------


def test_score_is_positive_finite_on_a_breakout():
    bars = make_session(date(2026, 1, 5), pattern="breakout_up")
    strat = RRBStrategy(_cfg())
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    assert len(longs) >= 1
    score = float(longs.iloc[0]["or_atr_ratio"])
    assert math.isfinite(score) and score > 0


def test_score_increases_monotonically_with_breakout_depth():
    """Load-bearing predictive-score test.

    The score formula is (close - rolling_high) / atr_bar. Holding the
    rolling reference fixed (same warm-up oscillation across fixtures) and
    holding the per-bar ATR approximately fixed, varying *only* the depth
    of the breakout close must produce a strictly increasing score.

    Mechanism this asserts: deeper closes through the reference level
    consume more overhead supply, and the score captures exactly that
    depth -- so the picker, when it ranks signals by this column, is
    ranking by a quantity with a direct mechanical link to the trade's
    edge-source. This is the bare minimum for the score to be doing work.
    """
    depths = [0.30, 0.50, 0.80, 1.20]
    scores: list[float] = []
    for d in depths:
        bars = make_session(
            date(2026, 1, 5),
            pattern="breakout_up",
            breakout_size=d,
        )
        strat = RRBStrategy(_cfg(cooldown_minutes=999))  # keep one entry only
        signals = strat.generate_signals(bars)
        longs = signals[signals["side"] == "long"]
        assert len(longs) == 1, (
            f"expected exactly one long entry for breakout_size={d}; got {longs}"
        )
        scores.append(float(longs.iloc[0]["or_atr_ratio"]))

    # Strictly increasing across the four levels.
    for i in range(1, len(scores)):
        assert scores[i] > scores[i - 1], (
            f"score must increase with depth: depths={depths}, scores={scores}"
        )


def test_score_equals_normalized_breakout_strength_for_long():
    """Exact formula check: score == (close - rolling_high_seen_by_strategy) / atr_bar.
    We don't recompute the rolling-high here (that would re-implement the
    strategy); instead we check the score is consistent with the entry's
    distance-to-stop, since both are scaled by the same ATR.

    Specifically: stop = close - atr_stop_multiplier * atr_bar, so
    (close - stop) / atr_stop_multiplier == atr_bar. The score is
    (close - rolling_high) / atr_bar, which must be a positive finite
    number with magnitude less than (close - lowest_warmup_low) / atr_bar
    -- i.e. the score is bounded by the obvious physical range.
    """
    bars = make_session(date(2026, 1, 5), pattern="breakout_up", breakout_size=0.5)
    cfg = _cfg(atr_stop_multiplier=1.0)
    strat = RRBStrategy(cfg)
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    assert len(longs) >= 1
    entry = longs.iloc[0]
    close = float(bars.loc[entry["timestamp"], "close"])
    atr_bar_implied = close - entry["stop_price"]  # since multiplier == 1.0
    score = float(entry["or_atr_ratio"])
    # Score must be positive and finite, and (score * atr_bar_implied) must
    # be a sensible price distance -- not larger than the breakout move
    # itself (close - base_price = 0.5).
    assert score > 0
    breakout_price_distance = score * atr_bar_implied
    assert 0 < breakout_price_distance <= 0.5 + 1e-6
