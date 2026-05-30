from __future__ import annotations

from dataclasses import replace
from datetime import date, time as dtime
from zoneinfo import ZoneInfo

from trading_bot.strategy import ORBStrategy, load_config

from .fixtures import make_multi_session, make_synthetic_session

ET = ZoneInfo("America/New_York")


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


def test_flat_by_et_none_holds_overnight_no_flat_signal():
    """flat_by_et=None disables the EOD flat (hold overnight) without affecting
    the entry. The default config keeps the flat — this is the opt-out path."""
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")

    held = ORBStrategy(_cfg(flat_by_et=None)).generate_signals(bars)
    assert held[held["side"] == "flat"].empty
    # Entry is unchanged vs the flat-enabled default.
    default = ORBStrategy(_cfg()).generate_signals(bars)
    assert len(held[held["side"] == "long"]) == len(default[default["side"] == "long"]) == 1
    assert not default[default["side"] == "flat"].empty  # default still flattens


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

    The pre-OR proxy is explicitly disabled here so the assertion remains
    a pure check of the OR-breakout min-range filter; the proxy path is
    covered by its own dedicated tests below.
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

    strat = ORBStrategy(
        _cfg(min_range_atr_multiplier=0.5, use_prior_close_proxy=False)
    )
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


# -------- pre-OR proxy band ------------------------------------------------
#
# Prior-session math used by the four tests below (kept here so the magic
# numbers in the fixtures are auditable):
#   Prior session built with default or_low=99, or_high=101, breakout="up":
#     - prior_close ≈ 101.86 (last bar close = 101.5 + 359*0.001)
#     - prior session range ≈ 2.91 (high_max ≈ 101.91, low_min = 99.0)
#   So with pre_or_k=0.5:
#     upper_edge ≈ 101.86 + 0.5*2.91 ≈ 103.31
#     lower_edge ≈ 101.86 - 0.5*2.91 ≈ 100.41


def test_pre_or_proxy_fires_long_inside_or_window():
    """With a prior session producing prior_close ≈ 101.86 and ATR ≈ 2.91,
    a today-open that prints closes above ≈ 103.31 during the 09:30–10:00
    ET window must fire exactly one long proxy entry. The current session is
    built with or_high=104 so its first-bar oscillation hits ≈ 103.75, which
    breaks the upper edge."""
    prior = make_synthetic_session(date(2026, 1, 5), breakout="up")
    today = make_synthetic_session(
        date(2026, 1, 6),
        breakout="up",
        or_high=104.0,
        or_low=99.0,
    )
    bars = make_multi_session([prior, today])

    # allow_short=False to keep the assertion focused on the long path —
    # the mirror short case is its own test.
    strat = ORBStrategy(_cfg(allow_short=False, use_prior_close_proxy=True))
    signals = strat.generate_signals(bars)

    today_date = date(2026, 1, 6)
    longs = signals[
        (signals["side"] == "long")
        & (signals["timestamp"].dt.date == today_date)
    ]
    assert len(longs) == 1
    entry = longs.iloc[0]
    entry_et = entry["timestamp"].tz_convert(ET).time()
    assert dtime(9, 30) <= entry_et < dtime(10, 0), (
        f"proxy entry must land in 09:30–10:00 ET, got {entry_et}"
    )
    assert entry["strategy"] == "orb"


def test_pre_or_proxy_fires_short_inside_or_window():
    """Mirror of the long test: prior_close ≈ 101.86, lower_edge ≈ 100.41.
    Today's first-bar oscillation must dip below 100.41. With
    or_high=100, or_low=95 the first close is ≈ 99.75, breaking below
    the lower edge."""
    prior = make_synthetic_session(date(2026, 1, 5), breakout="up")
    today = make_synthetic_session(
        date(2026, 1, 6),
        breakout="down",
        or_high=100.0,
        or_low=95.0,
    )
    bars = make_multi_session([prior, today])

    strat = ORBStrategy(_cfg(allow_short=True, use_prior_close_proxy=True))
    signals = strat.generate_signals(bars)

    today_date = date(2026, 1, 6)
    shorts = signals[
        (signals["side"] == "short")
        & (signals["timestamp"].dt.date == today_date)
    ]
    assert len(shorts) == 1
    entry = shorts.iloc[0]
    entry_et = entry["timestamp"].tz_convert(ET).time()
    assert dtime(9, 30) <= entry_et < dtime(10, 0), (
        f"proxy entry must land in 09:30–10:00 ET, got {entry_et}"
    )
    assert entry["strategy"] == "orb"


def test_pre_or_proxy_silent_without_prior_session_data():
    """Graceful fallback: a single session (empty prior-session deques) must
    not fire any proxy entries — the proxy block requires both
    prior_session_closes and prior_session_ranges to be non-empty. The
    standard OR-breakout path after 10:00 must still produce its one long
    entry, demonstrating the strategy degrades gracefully to the old
    single-OR-window behavior."""
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    strat = ORBStrategy(_cfg(use_prior_close_proxy=True))
    signals = strat.generate_signals(bars)

    entries = signals[signals["side"].isin(["long", "short"])]
    assert len(entries) == 1
    entry = entries.iloc[0]
    entry_et = entry["timestamp"].tz_convert(ET).time()
    # The lone entry must come from the OR-breakout phase (>= 10:00 ET),
    # never from the proxy phase.
    assert entry_et >= dtime(10, 0), (
        f"with no prior-session data, entries must be OR-breakout (>= 10:00 ET); "
        f"got {entry_et}"
    )
    assert entry["side"] == "long"


def test_pre_or_proxy_does_not_double_fire_with_or_breakout():
    """The `long_done` flag must be honored across the proxy → OR-breakout
    phase boundary. Construct a setup where the 09:30 proxy fires AND the
    after-10:00 bars would otherwise produce a clean OR-breakout long: only
    ONE long entry must be emitted (the earlier proxy fire pre-empts the
    OR-breakout for the same session)."""
    prior = make_synthetic_session(date(2026, 1, 5), breakout="up")
    # Today's session: first-bar close ≈ 103.75 breaks the proxy upper edge,
    # AND after 10:00 the up-breakout continues so the OR-breakout phase
    # would otherwise also fire (closes drift well above or_high=104).
    today = make_synthetic_session(
        date(2026, 1, 6),
        breakout="up",
        or_high=104.0,
        or_low=99.0,
    )
    bars = make_multi_session([prior, today])

    strat = ORBStrategy(_cfg(allow_short=False, use_prior_close_proxy=True))
    signals = strat.generate_signals(bars)

    today_date = date(2026, 1, 6)
    longs = signals[
        (signals["side"] == "long")
        & (signals["timestamp"].dt.date == today_date)
    ]
    assert len(longs) == 1, (
        f"expected exactly one long (proxy pre-empts OR-breakout), got "
        f"{len(longs)}:\n{longs}"
    )
    entry_et = longs.iloc[0]["timestamp"].tz_convert(ET).time()
    assert entry_et < dtime(10, 0), (
        f"the surviving long must be the 09:30–10:00 proxy fire, got {entry_et}"
    )
