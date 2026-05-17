from __future__ import annotations

from dataclasses import replace
from datetime import date

from trading_bot.strategy import ORBStrategy, load_config

from .fixtures import make_multi_session, make_synthetic_session


def _cfg(**overrides):
    cfg = load_config()
    return replace(cfg, **overrides)


def test_up_breakout_produces_one_long_entry():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    strat = ORBStrategy(_cfg())
    signals = strat.generate_signals(bars)

    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1
    entry = longs.iloc[0]
    assert entry["stop_price"] < bars.loc[entry["timestamp"], "close"]
    assert entry["take_price"] > bars.loc[entry["timestamp"], "close"]


def test_down_breakout_produces_one_short_entry():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="down")
    strat = ORBStrategy(_cfg(allow_short=True))
    signals = strat.generate_signals(bars)

    shorts = signals[signals["side"] == "short"]
    assert len(shorts) == 1
    entry = shorts.iloc[0]
    assert entry["stop_price"] > bars.loc[entry["timestamp"], "close"]
    assert entry["take_price"] < bars.loc[entry["timestamp"], "close"]


def test_no_breakout_produces_no_entries():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="none")
    strat = ORBStrategy(_cfg())
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0
    flats = signals[signals["side"] == "flat"]
    assert len(flats) == 1


def test_allow_short_false_blocks_short_entry():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="down")
    strat = ORBStrategy(_cfg(allow_short=False))
    signals = strat.generate_signals(bars)

    assert (signals["side"] != "short").all()


def test_flat_range_session_skipped():
    bars = make_synthetic_session(date(2026, 1, 5), flat_range=True, breakout="up")
    strat = ORBStrategy(_cfg(min_range_atr_multiplier=10.0))
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0


def test_target_size_pct_matches_config():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    cfg = _cfg(target_size_pct=0.07)
    strat = ORBStrategy(cfg)
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"] != "flat"]
    assert (entries["target_size_pct"] == 0.07).all()


def test_take_r_multiple_respected():
    import pytest

    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    strat = ORBStrategy(_cfg(take_r_multiple=3.0))
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1
    entry = longs.iloc[0]
    close = bars.loc[entry["timestamp"], "close"]
    risk = close - entry["stop_price"]
    reward = entry["take_price"] - close
    assert reward == pytest.approx(3.0 * risk, rel=1e-9)


def test_missing_symbol_raises():
    import pytest

    bars = make_synthetic_session(date(2026, 1, 5), breakout="up").drop(columns=["symbol"])
    strat = ORBStrategy(load_config())
    with pytest.raises(ValueError, match="symbol"):
        strat.generate_signals(bars)


def test_min_range_filter_uses_session_scale_atr():
    """Regression: the min-range filter must be denominated in session-scale
    ATR (rolling mean of prior-session high-low ranges), not per-minute TR.

    Construct a wide prior session (range ~10) and a flat current session
    (OR range ~0.05). With min_range_atr_multiplier=0.5 the threshold is
    0.5 * 10 = 5.0, far above the 0.05 OR — the current session must be
    rejected (no long/short entries).
    """
    wide_prior = make_synthetic_session(
        date(2026, 1, 5),
        breakout="up",
        or_low=95.0,
        or_high=105.0,
    )
    flat_current = make_synthetic_session(
        date(2026, 1, 6),
        breakout="up",
        flat_range=True,
    )
    bars = make_multi_session([wide_prior, flat_current])

    strat = ORBStrategy(_cfg(min_range_atr_multiplier=0.5))
    signals = strat.generate_signals(bars)

    current_day = date(2026, 1, 6)
    entries = signals[
        (signals["side"].isin(["long", "short"]))
        & (signals["timestamp"].dt.date == current_day)
    ]
    assert len(entries) == 0


def test_filter_audit_log_emits_or_atr_and_cold_start(caplog):
    """The min-range filter must log OR range, ATR, ratio, and cold-start
    status per session-symbol so post-mortems can audit whether the cold-start
    fallback was disarming the filter (flagged in 2026-05-14 review)."""
    import logging
    import math

    bars = make_synthetic_session(date(2026, 1, 5), breakout="up", symbol="SPY")
    strat = ORBStrategy(_cfg())
    with caplog.at_level(logging.INFO, logger="trading_bot.strategy.orb.strategy"):
        strat.generate_signals(bars)
    audit = [r for r in caplog.records if r.message == "ORB filter check"]
    assert audit, "expected the ORB filter check audit record"
    rec = audit[0]
    # Structured fields are surfaced on the record via the `extra` mechanism.
    assert rec.symbol == "SPY"
    assert rec.session_date == "2026-01-05"
    assert rec.buffer_n == 0
    assert rec.cold_start is True
    # Cold-start by construction → atr falls back to or_range → ratio == 1.0.
    # This is the disarming behavior the review flagged; pin it so a future
    # change to the cold-start fallback can't silently slip past.
    assert math.isclose(rec.ratio, 1.0, rel_tol=1e-9)
    assert rec.or_high > rec.or_low
    assert math.isclose(rec.or_range, rec.or_high - rec.or_low, rel_tol=1e-9)


def test_latest_entry_et_suppresses_late_day_entries():
    """A breakout-style tape with latest_entry_et set early should produce
    no entries (the cutoff fires before the breakout window). Today's tape
    (2026-05-14) had 10:01-10:49 ET breakouts entered at 13:57 ET — exactly
    the late-day pattern this knob is designed to suppress."""
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    # Cutoff at 09:45 ET → before the 10:00 ET OR window ends. No entry can fire.
    strat = ORBStrategy(_cfg(latest_entry_et="09:45"))
    signals = strat.generate_signals(bars)
    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 0
    # Flat signal is NOT gated by latest_entry_et — it still fires at 15:55 ET.
    flats = signals[signals["side"] == "flat"]
    assert len(flats) == 1


def test_latest_entry_et_none_disables_filter():
    """latest_entry_et=None → no time gate; entry fires as before."""
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    strat = ORBStrategy(_cfg(latest_entry_et=None))
    signals = strat.generate_signals(bars)
    assert len(signals[signals["side"] == "long"]) == 1


def test_latest_entry_et_after_breakout_does_not_block():
    """Cutoff at 11:00 ET → after the 10:01 breakout. Entry still fires."""
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    strat = ORBStrategy(_cfg(latest_entry_et="11:00"))
    signals = strat.generate_signals(bars)
    assert len(signals[signals["side"] == "long"]) == 1


def test_cold_start_does_not_block_normal_breakout():
    """On the very first session in a sequence (empty prior-range buffer) the
    filter falls back to using the OR range itself as the ATR. With the
    default 0.5 multiplier, or_range / atr == 1.0, so a normal breakout
    should still fire.
    """
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    strat = ORBStrategy(_cfg(min_range_atr_multiplier=0.5))
    signals = strat.generate_signals(bars)
    longs = signals[signals["side"] == "long"]
    assert len(longs) == 1
