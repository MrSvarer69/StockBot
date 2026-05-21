"""Unit tests for VWAPStrategy v2.

Pin behavior on small synthetic fixtures. No I/O, no broker, no network --
the strategy is a pure function and these tests stay that way too.

Trigger-mechanic tests pass ``allow_against_trend=True`` to isolate the
reclaim/rejection logic from the daily-trend regime gate. Trend-gate
behavior gets its own section using ``make_multi_session`` fixtures that
construct N prior sessions encoding a deterministic trend.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from trading_bot.strategy.vwap.strategy import VWAPStrategy, load_config

from .fixtures import make_multi_session, make_session


def _cfg(**overrides):
    """Default config but with the trend gate disabled, so trigger-mechanic
    tests can use a single-session fixture without satisfying the daily
    trend filter. Tests that exercise the trend filter explicitly re-enable
    it via overrides."""
    cfg = load_config()
    overrides.setdefault("allow_against_trend", True)
    return replace(cfg, **overrides)


# ---------------------------------------------------------------------------
# Signal rule firing
# ---------------------------------------------------------------------------


def test_reclaim_up_produces_one_long_entry():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    strat = VWAPStrategy(_cfg())
    signals = strat.generate_signals(bars)

    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1, signals
    entry = longs.iloc[0]
    assert entry["stop_price"] < entry["take_price"]
    assert pd.notna(entry["or_atr_ratio"]) and entry["or_atr_ratio"] > 0


def test_rejection_down_produces_one_short_entry():
    bars = make_session(date(2026, 1, 5), pattern="rejection_down")
    strat = VWAPStrategy(_cfg(allow_short=True))
    signals = strat.generate_signals(bars)

    shorts = signals[signals["side"] == "short"]
    assert len(shorts) == 1, signals
    entry = shorts.iloc[0]
    assert entry["stop_price"] > entry["take_price"]


# ---------------------------------------------------------------------------
# No-fire conditions
# ---------------------------------------------------------------------------


def test_flat_tape_produces_no_entries():
    bars = make_session(date(2026, 1, 5), pattern="flat")
    strat = VWAPStrategy(_cfg())
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0
    flats = signals[signals["side"] == "flat"]
    assert len(flats) == 1


def test_shallow_dip_below_min_extension_does_not_fire():
    bars = make_session(date(2026, 1, 5), pattern="shallow_dip")
    strat = VWAPStrategy(_cfg(min_extension_bars=5))
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0


def test_allow_short_false_blocks_short_entry():
    bars = make_session(date(2026, 1, 5), pattern="rejection_down")
    strat = VWAPStrategy(_cfg(allow_short=False))
    signals = strat.generate_signals(bars)

    assert (signals["side"] != "short").all()


def test_latest_entry_et_suppresses_late_day_entries():
    bars = make_session(
        date(2026, 1, 5),
        pattern="reclaim_up",
        reclaim_start_bar=240,
    )
    strat = VWAPStrategy(_cfg(latest_entry_et="10:00"))
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0
    flats = signals[signals["side"] == "flat"]
    assert len(flats) == 1


def test_cooldown_prevents_back_to_back_entries():
    bars_a = make_session(
        date(2026, 1, 5),
        pattern="reclaim_up",
        reclaim_start_bar=60,
    )
    closes = bars_a["close"].to_numpy(dtype="float64").copy()
    base_price = 100.0
    for i in range(150, 165):
        closes[i] = base_price - 0.5
    for i in range(165, 200):
        closes[i] = base_price + 0.6
    bars_a = bars_a.assign(close=closes)

    strat = VWAPStrategy(_cfg(cooldown_minutes=120))
    signals = strat.generate_signals(bars_a)
    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1


# ---------------------------------------------------------------------------
# Trend regime filter (new in v2)
# ---------------------------------------------------------------------------


def test_long_reclaim_blocked_when_daily_trend_is_down():
    """Reclaim-up pattern with prior sessions encoding a downtrend: the
    daily-trend gate must block the long."""
    bars = make_multi_session(
        date(2026, 2, 17),  # Tuesday
        pattern="reclaim_up",
        trend="down",
        n_prior_sessions=25,
    )
    strat = VWAPStrategy(load_config())  # default = gate ON
    signals = strat.generate_signals(bars)

    longs = signals[signals["side"] == "long"]
    assert len(longs) == 0, signals


def test_long_reclaim_allowed_when_daily_trend_is_up():
    bars = make_multi_session(
        date(2026, 2, 17),
        pattern="reclaim_up",
        trend="up",
        n_prior_sessions=25,
    )
    strat = VWAPStrategy(load_config())
    signals = strat.generate_signals(bars)

    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1, signals


def test_short_rejection_blocked_when_daily_trend_is_up():
    bars = make_multi_session(
        date(2026, 2, 17),
        pattern="rejection_down",
        trend="up",
        n_prior_sessions=25,
    )
    strat = VWAPStrategy(load_config())
    signals = strat.generate_signals(bars)

    shorts = signals[signals["side"] == "short"]
    assert len(shorts) == 0, signals


def test_short_rejection_allowed_when_daily_trend_is_down():
    bars = make_multi_session(
        date(2026, 2, 17),
        pattern="rejection_down",
        trend="down",
        n_prior_sessions=25,
    )
    strat = VWAPStrategy(load_config())
    signals = strat.generate_signals(bars)

    shorts = signals[signals["side"] == "short"]
    assert len(shorts) == 1, signals


def test_unknown_regime_suppresses_both_directions_by_default():
    """Single session (no priors): regime is "unknown". Default config must
    suppress signals on both sides."""
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    strat = VWAPStrategy(load_config())
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0


def test_allow_against_trend_true_reverts_to_unfiltered_behavior():
    """Override: with the gate disabled, a single-session reclaim_up still
    fires even though the regime is unknown."""
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    strat = VWAPStrategy(_cfg(allow_against_trend=True))
    signals = strat.generate_signals(bars)

    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1


# ---------------------------------------------------------------------------
# Stop / take computation -- asymmetric by construction (v2)
# ---------------------------------------------------------------------------


def test_long_stop_and_take_use_atr_and_r_multiple():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    strat = VWAPStrategy(_cfg(atr_stop_multiplier=1.0, take_r_multiple=1.5))
    signals = strat.generate_signals(bars)

    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1
    entry = longs.iloc[0]
    close = float(bars.loc[entry["timestamp"], "close"])
    risk = close - entry["stop_price"]
    reward = entry["take_price"] - close
    assert risk > 0
    assert reward == pytest.approx(1.5 * risk, rel=1e-9)


def test_short_stop_and_take_mirror_long():
    bars = make_session(date(2026, 1, 5), pattern="rejection_down")
    strat = VWAPStrategy(_cfg(atr_stop_multiplier=1.0, take_r_multiple=1.5))
    signals = strat.generate_signals(bars)

    shorts = signals[signals["side"] == "short"]
    assert len(shorts) == 1
    entry = shorts.iloc[0]
    close = float(bars.loc[entry["timestamp"], "close"])
    risk = entry["stop_price"] - close
    reward = close - entry["take_price"]
    assert risk > 0
    assert reward == pytest.approx(1.5 * risk, rel=1e-9)


def test_default_config_is_asymmetric_with_take_greater_than_stop_distance():
    """Default config must give a >1 reward/risk ratio, not symmetric."""
    cfg = load_config()
    assert cfg.take_r_multiple > 1.0, (
        "v2 default must provide asymmetric reward; "
        f"take_r_multiple={cfg.take_r_multiple}"
    )


def test_both_stop_and_take_set_on_every_entry():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    strat = VWAPStrategy(_cfg())
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
# Pure-function behavior
# ---------------------------------------------------------------------------


def test_strategy_does_not_mutate_input_bars():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    snapshot = bars.copy(deep=True)
    strat = VWAPStrategy(_cfg())
    strat.generate_signals(bars)
    pd.testing.assert_frame_equal(bars, snapshot)


def test_same_input_yields_identical_output():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    strat = VWAPStrategy(_cfg())
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
    strat = VWAPStrategy(_cfg())
    out = strat.generate_signals(empty)
    assert len(out) == 0
    for col in (
        "timestamp", "symbol", "side", "target_size_pct",
        "stop_price", "take_price", "or_atr_ratio",
    ):
        assert col in out.columns


def test_missing_symbol_raises():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up").drop(columns=["symbol"])
    strat = VWAPStrategy(_cfg())
    with pytest.raises(ValueError, match="symbol"):
        strat.generate_signals(bars)


def test_naive_index_raises():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    bars.index = bars.index.tz_convert("UTC").tz_localize(None)
    strat = VWAPStrategy(_cfg())
    with pytest.raises(ValueError, match="tz-aware"):
        strat.generate_signals(bars)


# ---------------------------------------------------------------------------
# Score: extension_depth * (1 + clip(volume_decay, 0, 1))
# ---------------------------------------------------------------------------


def test_score_is_positive_finite_on_a_reclaim():
    bars = make_session(date(2026, 1, 5), pattern="reclaim_up")
    strat = VWAPStrategy(_cfg())
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1
    score = float(longs.iloc[0]["or_atr_ratio"])
    assert math.isfinite(score) and score > 0


def test_score_higher_with_volume_decay_during_extension():
    """Property: a reclaim whose extension exhibits declining volume should
    score strictly higher than the same reclaim with flat volume. Same
    closes/HLO across both fixtures, so extension_depth is held constant."""
    base = make_session(date(2026, 1, 5), pattern="reclaim_up")
    decayed = make_session(
        date(2026, 1, 5),
        pattern="reclaim_up",
        extension_volume_decay=True,
    )
    strat = VWAPStrategy(_cfg())
    base_sig = strat.generate_signals(base)
    decayed_sig = strat.generate_signals(decayed)

    base_long = base_sig[base_sig["side"] == "long"]
    decayed_long = decayed_sig[decayed_sig["side"] == "long"]
    assert len(base_long) == 1 and len(decayed_long) == 1
    base_score = float(base_long.iloc[0]["or_atr_ratio"])
    decayed_score = float(decayed_long.iloc[0]["or_atr_ratio"])
    # Decay multiplier is in [1, 2]; flat volume should give exactly 1x,
    # decaying volume should give strictly >1x.
    assert decayed_score > base_score


def test_score_multiplier_bounded_in_one_to_two_range():
    """The score formula is depth * (1 + clip(decay, 0, 1)), so the
    multiplier sits in [1.0, 2.0]. The decayed-volume fixture must score
    strictly higher than the flat-volume baseline, and never more than 2x
    the depth (i.e. never more than 2x the flat-volume baseline, since the
    depths are approximately equal between the two fixtures).
    """
    flat = make_session(date(2026, 1, 5), pattern="reclaim_up")
    decayed = make_session(
        date(2026, 1, 5),
        pattern="reclaim_up",
        extension_volume_decay=True,
    )
    strat = VWAPStrategy(_cfg())
    flat_score = float(
        strat.generate_signals(flat).query("side == 'long'").iloc[0]["or_atr_ratio"]
    )
    decayed_score = float(
        strat.generate_signals(decayed).query("side == 'long'").iloc[0]["or_atr_ratio"]
    )
    # Strictly more than 1x (volume actually decayed): score is boosted.
    assert decayed_score > flat_score
    # At most 2x the baseline depth: multiplier is bounded by 2.
    assert decayed_score <= 2.0 * flat_score + 1e-9
