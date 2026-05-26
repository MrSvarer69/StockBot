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


# --------------------------------------------------------------------- warmup
# The block below covers the prior-session warmup wiring: setter contract,
# date gate, no-data fallback, and the disable flag. The construction
# pattern in tests 1 and 4 is a hand-rolled ramp-then-pullback prior
# session combined with a continuation ramp today, so the strategy's
# combined-frame EMA arithmetic is fully deterministic.

ET_TZ = "America/New_York"


def _build_warmup_prior_and_today(
    *,
    prior_date,
    today_date,
    base_price: float = 100.0,
    trend_slope: float = 0.04,
    pullback_start_bar: int = 380,
    pullback_bars: int = 8,
    pullback_depth: float = 1.5,
    today_bars: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (prior, today) frames where prior ramps -> pulls back ->
    starts the reclaim in its final two bars, and today continues that
    ramp from the open. EMA_fast warmth from prior is what lets a
    pullback entry fire at/near today's first bar; without warmup the
    same today frame is too short to warm a 50-bar EMA.

    With ``pullback_start_bar=380`` and ``pullback_bars=8``, prior
    bars 380..387 sit at the pullback floor and bars 388..389 step
    back onto the ramp -- those two bars satisfy
    ``reclaim_confirm_bars=2`` so today's 09:30 bar carries the entry
    once the pullback-lookback window still contains the wrong-side
    bars 380..387.
    """
    prior_start_utc = (
        pd.Timestamp.combine(prior_date, pd.Timestamp("09:30").time())
        .tz_localize(ET_TZ)
        .tz_convert("UTC")
    )
    n_prior = 6 * 60 + 30
    prior_idx = pd.DatetimeIndex(
        pd.date_range(prior_start_utc, periods=n_prior, freq="1min", tz="UTC"),
        name="timestamp",
    )
    closes_p: list[float] = []
    for i in range(n_prior):
        if i < pullback_start_bar:
            closes_p.append(base_price + i * trend_slope)
        elif i < pullback_start_bar + pullback_bars:
            closes_p.append(
                base_price + pullback_start_bar * trend_slope - pullback_depth
            )
        else:
            closes_p.append(base_price + i * trend_slope)
    opens_p = [closes_p[0]] + closes_p[:-1]
    highs_p = [max(o, c) + 0.05 for o, c in zip(opens_p, closes_p)]
    lows_p = [min(o, c) - 0.05 for o, c in zip(opens_p, closes_p)]
    prior = pd.DataFrame(
        {
            "open": opens_p,
            "high": highs_p,
            "low": lows_p,
            "close": closes_p,
            "volume": [1000.0] * n_prior,
            "symbol": ["SPY"] * n_prior,
        },
        index=prior_idx,
    )

    # Today: clean ramp from prior's last close so the very first bar
    # closes above the (warm) EMA_fast -- this IS the reclaim.
    today_start_utc = (
        pd.Timestamp.combine(today_date, pd.Timestamp("09:30").time())
        .tz_localize(ET_TZ)
        .tz_convert("UTC")
    )
    today_idx = pd.DatetimeIndex(
        pd.date_range(today_start_utc, periods=today_bars, freq="1min", tz="UTC"),
        name="timestamp",
    )
    last_close = closes_p[-1]
    closes_t = [last_close + (i + 1) * trend_slope for i in range(today_bars)]
    opens_t = [closes_t[0]] + closes_t[:-1]
    highs_t = [max(o, c) + 0.05 for o, c in zip(opens_t, closes_t)]
    lows_t = [min(o, c) - 0.05 for o, c in zip(opens_t, closes_t)]
    today = pd.DataFrame(
        {
            "open": opens_t,
            "high": highs_t,
            "low": lows_t,
            "close": closes_t,
            "volume": [1000.0] * today_bars,
            "symbol": ["SPY"] * today_bars,
        },
        index=today_idx,
    )
    return prior, today


def test_warmup_enables_entry_at_open_when_prior_bars_provided():
    """Prior-session warmup lets the pullback fire at/near 09:30 ET.

    Build a prior session that ramps then pulls back through the final
    bars (no in-session reclaim), and a today frame whose first bar
    closes back above the warm EMA_fast -- i.e., the reclaim happens
    on today's 09:30 bar. ``trend_slope_lookback=2`` keeps the slope-
    back comparison inside the pullback region so EMA_fast slope is
    rising at 09:30. The same today bars fed without warmup must
    produce zero entries; that contrast is what proves the warmup
    mechanic, not the entry-exists assertion alone.
    """
    prior, today = _build_warmup_prior_and_today(
        prior_date=date(2026, 5, 4),
        today_date=date(2026, 5, 5),
    )
    cfg = _cfg(
        earliest_entry_et="09:30",
        latest_entry_et="15:30",
        trend_slope_lookback=2,
    )

    warm = PullbackStrategy(cfg)
    warm.set_prior_session_bars({"SPY": prior})
    sigs = warm.generate_signals(today)
    entries = sigs[sigs["side"].isin(["long", "short"])]
    assert len(entries) >= 1, (
        f"expected at least one entry with warmup, got 0: "
        f"{sigs[['timestamp','side']].to_dict('records')}"
    )
    first_et = entries.iloc[0]["timestamp"].tz_convert(ET_TZ).time()
    assert first_et <= pd.Timestamp("09:35").time(), (
        f"warmup should let the entry fire at/near 09:30 ET, got {first_et}"
    )

    # Contrast: same today bars without prior context never fire --
    # the today frame is too short for the 50-bar EMA to warm on its
    # own. This is what makes the warmup behaviour load-bearing.
    cold = PullbackStrategy(cfg)
    cold_sigs = cold.generate_signals(today)
    cold_entries = cold_sigs[cold_sigs["side"].isin(["long", "short"])]
    assert cold_entries.empty, (
        f"without warmup, today alone must not fire; got "
        f"{cold_entries[['timestamp','side']].to_dict('records')}"
    )


def test_prior_session_bars_never_emit_entries_on_their_date():
    """The date gate suppresses entry rows on any prior-session bar.

    The prior frame here is the canonical ``trend_up_pullback_reclaim``
    fixture that already fires a long around 12:00 ET when fed alone.
    Today's frame is a flat oscillation centered on prior's last close
    -- it does not produce its own entries either. With warmup wired,
    the combined frame must produce zero entry rows on either date;
    only the daily flat row remains.
    """
    prior = make_session(
        date(2026, 5, 4),
        phase="trend_up_pullback_reclaim",
        pullback_start_bar=150,
        pullback_bars=6,
        pullback_depth=1.5,
    )
    last_close = float(prior["close"].iloc[-1])

    today_start_utc = (
        pd.Timestamp.combine(date(2026, 5, 5), pd.Timestamp("09:30").time())
        .tz_localize(ET_TZ)
        .tz_convert("UTC")
    )
    n_today = 6 * 60 + 30
    today_idx = pd.DatetimeIndex(
        pd.date_range(today_start_utc, periods=n_today, freq="1min", tz="UTC"),
        name="timestamp",
    )
    closes_t = [last_close + ((i % 2) * 0.02 - 0.01) for i in range(n_today)]
    opens_t = [closes_t[0]] + closes_t[:-1]
    highs_t = [max(o, c) + 0.05 for o, c in zip(opens_t, closes_t)]
    lows_t = [min(o, c) - 0.05 for o, c in zip(opens_t, closes_t)]
    today = pd.DataFrame(
        {
            "open": opens_t,
            "high": highs_t,
            "low": lows_t,
            "close": closes_t,
            "volume": [1000.0] * n_today,
            "symbol": ["SPY"] * n_today,
        },
        index=today_idx,
    )

    cfg = _cfg(
        earliest_entry_et="09:30",
        latest_entry_et="15:30",
    )
    strat = PullbackStrategy(cfg)
    strat.set_prior_session_bars({"SPY": prior})
    sigs = strat.generate_signals(today)

    entries = sigs[sigs["side"].isin(["long", "short"])]
    assert entries.empty, (
        f"prior-session firing pattern leaked through the date gate: "
        f"{entries[['timestamp','side']].to_dict('records')}"
    )
    # The only remaining row should be the daily flat, dated today.
    flats = sigs[sigs["side"] == "flat"]
    assert len(flats) == 1
    flat_date = flats.iloc[0]["timestamp"].tz_convert(ET_TZ).date()
    assert flat_date == date(2026, 5, 5)


def test_no_prior_bars_falls_back_to_today_only_behavior():
    """An empty prior frame is identical to no warmup at all.

    Production-without-warmup needs the in-session EMAs to warm
    naturally over ~bar 60 (ema_slow_period + trend_slope_lookback).
    A pullback-reclaim pattern starting at bar 150 fires in late
    morning -- well after 09:30. Setting an empty DataFrame as prior
    must not crash, must not warn, and must produce the same result.
    """
    today = make_session(
        date(2026, 5, 5),
        phase="trend_up_pullback_reclaim",
        pullback_start_bar=150,
        pullback_bars=6,
        pullback_depth=1.5,
    )
    cfg = _cfg(earliest_entry_et="09:30", latest_entry_et="15:30")

    import warnings

    # Variant A: explicit empty frame for SPY.
    strat_a = PullbackStrategy(cfg)
    strat_a.set_prior_session_bars({"SPY": pd.DataFrame()})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sigs_a = strat_a.generate_signals(today)
    # No warnings about missing prior data (allow unrelated third-party warnings).
    pullback_warnings = [
        w
        for w in caught
        if "prior" in str(w.message).lower() or "warmup" in str(w.message).lower()
    ]
    assert not pullback_warnings, (
        f"unexpected warning about missing prior data: {pullback_warnings}"
    )
    entries_a = sigs_a[sigs_a["side"].isin(["long", "short"])]
    assert not entries_a.empty, (
        "empty-prior fallback should still fire the in-session entry"
    )
    first_et_a = entries_a.iloc[0]["timestamp"].tz_convert(ET_TZ).time()
    assert first_et_a >= pd.Timestamp("10:30").time(), (
        f"without real warmup, entry must wait for in-session EMA to warm; "
        f"got {first_et_a}"
    )

    # Variant B: mapping that contains nothing for SPY at all.
    strat_b = PullbackStrategy(cfg)
    strat_b.set_prior_session_bars({})
    sigs_b = strat_b.generate_signals(today)
    entries_b = sigs_b[sigs_b["side"].isin(["long", "short"])]
    assert not entries_b.empty
    first_et_b = entries_b.iloc[0]["timestamp"].tz_convert(ET_TZ).time()
    assert first_et_b >= pd.Timestamp("10:30").time()

    # Both variants must agree -- the absence-of-data branch is one path.
    assert first_et_a == first_et_b


def test_warmup_disabled_makes_setter_a_noop():
    """When ``warmup_from_prior_session=False``, the setter is a no-op.

    Reusing the test-1 construction (which fires at 09:30 with warmup),
    flip the flag off and confirm no entry fires near the open: today's
    30 bars alone cannot warm the 50-bar EMA, so the strategy has no
    indicator signal to act on.
    """
    prior, today = _build_warmup_prior_and_today(
        prior_date=date(2026, 5, 4),
        today_date=date(2026, 5, 5),
    )
    cfg = _cfg(
        earliest_entry_et="09:30",
        latest_entry_et="15:30",
        trend_slope_lookback=2,
    )
    cfg = replace(cfg, warmup_from_prior_session=False)

    strat = PullbackStrategy(cfg)
    strat.set_prior_session_bars({"SPY": prior})  # must no-op
    sigs = strat.generate_signals(today)
    entries = sigs[sigs["side"].isin(["long", "short"])]
    # Either zero entries, or any entry must NOT be at/near 09:30 (the
    # warmup-only entry). With only 30 today bars and a 50-bar EMA,
    # zero entries is the expected outcome.
    if not entries.empty:
        first_et = entries.iloc[0]["timestamp"].tz_convert(ET_TZ).time()
        assert first_et > pd.Timestamp("09:35").time(), (
            f"flag-disabled setter should not surface a 09:30 entry; "
            f"got {first_et}"
        )
