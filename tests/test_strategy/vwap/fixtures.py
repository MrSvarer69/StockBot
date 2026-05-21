"""Synthetic 1-min bars for VWAP strategy tests.

The fixtures build sessions whose VWAP is approximately anchored to a known
``base_price`` so test assertions can reason about "above VWAP" / "below
VWAP" without recomputing the indicator. To keep VWAP itself near the base,
the construction interleaves opening bars symmetrically around the base
price before introducing an extension/reclaim pattern.

For v2 trend-filter tests, ``make_multi_session`` prepends N synthetic prior
sessions whose closes encode a deterministic up- or down-trend, so the
strategy's daily-SMA comparison evaluates predictably.
"""

from __future__ import annotations

from datetime import date as date_type, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

Pattern = Literal[
    "reclaim_up",       # extension below VWAP, then reclaim above
    "rejection_down",   # extension above VWAP, then reject below
    "flat",             # closes hug the base price; no extension
    "shallow_dip",      # one-bar dip below VWAP, no min_extension_bars trigger
]


def make_session(
    session_date: date_type,
    *,
    pattern: Pattern = "reclaim_up",
    base_price: float = 100.0,
    extension_bars: int = 10,
    extension_size: float = 0.5,
    reclaim_size: float = 0.6,
    reclaim_start_bar: int = 60,
    symbol: str = "SPY",
    extension_volume_decay: bool = False,
) -> pd.DataFrame:
    """Build a single 09:30-16:00 ET session of 1-min UTC bars.

    The first 30 bars oscillate tightly around ``base_price`` to anchor VWAP.
    From ``reclaim_start_bar``:
      - ``reclaim_up`` : ``extension_bars`` bars at base - extension_size (below
        VWAP), then a sustained move to base + reclaim_size (above VWAP).
      - ``rejection_down`` : mirrored.
      - ``flat`` : continues to hug base; no clear extension or reclaim.
      - ``shallow_dip`` : one bar at base - extension_size, then back above.

    If ``extension_volume_decay`` is True, volume during the extension window
    halves in its second half (simulating exhaustion). This is used by the
    score test to assert the volume-decay multiplier kicks in.
    """
    start = pd.Timestamp.combine(session_date, pd.Timestamp("09:30").time())
    start = start.tz_localize(ET).tz_convert("UTC")
    n = 6 * 60 + 30  # 390 minutes
    idx = pd.DatetimeIndex(
        pd.date_range(start, periods=n, freq="1min", tz="UTC"), name="timestamp"
    )

    closes: list[float] = []
    volumes: list[float] = [1000.0] * n
    for i in range(n):
        if i < reclaim_start_bar:
            sign = 1 if (i % 2 == 0) else -1
            closes.append(base_price + sign * 0.02)
            continue

        offset_in_pattern = i - reclaim_start_bar

        if pattern == "reclaim_up":
            if offset_in_pattern < extension_bars:
                closes.append(base_price - extension_size)
                if extension_volume_decay:
                    # First half of extension: high volume; second half: low.
                    if offset_in_pattern >= extension_bars // 2:
                        volumes[i] = 400.0
                    else:
                        volumes[i] = 1600.0
            else:
                closes.append(base_price + reclaim_size)
        elif pattern == "rejection_down":
            if offset_in_pattern < extension_bars:
                closes.append(base_price + extension_size)
                if extension_volume_decay:
                    if offset_in_pattern >= extension_bars // 2:
                        volumes[i] = 400.0
                    else:
                        volumes[i] = 1600.0
            else:
                closes.append(base_price - reclaim_size)
        elif pattern == "flat":
            sign = 1 if (i % 2 == 0) else -1
            closes.append(base_price + sign * 0.02)
        elif pattern == "shallow_dip":
            if offset_in_pattern == 0:
                closes.append(base_price - extension_size)
            else:
                closes.append(base_price + reclaim_size)
        else:  # pragma: no cover
            raise ValueError(f"unknown pattern: {pattern}")

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.05 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.05 for o, c in zip(opens, closes)]

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "symbol": [symbol] * n,
        },
        index=idx,
    )


def make_prior_sessions(
    end_before_date: date_type,
    *,
    n_sessions: int,
    trend: Literal["up", "down", "flat"],
    base_price: float = 100.0,
    daily_step: float = 0.2,
    symbol: str = "SPY",
) -> pd.DataFrame:
    """Build ``n_sessions`` synthetic 1-min sessions ending the trading day
    immediately before ``end_before_date``, whose closes encode a monotonic
    trend.

    A ``trend="up"`` session block has each session's close ``daily_step``
    higher than the prior; ``"down"`` is mirrored. The SMA of these closes
    will satisfy "last > SMA" for up and "last < SMA" for down, as long as
    ``n_sessions`` is at least the SMA period.

    Weekday-only: the function skips Saturdays and Sundays when stepping
    back from ``end_before_date``.
    """
    # Walk back ``n_sessions`` weekdays from end_before_date - 1.
    days: list[date_type] = []
    d = end_before_date - timedelta(days=1)
    while len(days) < n_sessions:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()  # chronological order

    frames: list[pd.DataFrame] = []
    for i, day in enumerate(days):
        if trend == "up":
            price = base_price + daily_step * (i + 1)
        elif trend == "down":
            price = base_price - daily_step * (i + 1)
        else:
            price = base_price
        # Each prior session: flat tape at the day's price (just enough to
        # set today's close). The trend filter only consumes the final close.
        sess = make_session(
            day, pattern="flat", base_price=price, symbol=symbol,
        )
        # Force the closing bar to exactly ``price`` so the deque value is
        # deterministic regardless of the flat oscillation.
        closes = sess["close"].to_numpy(dtype="float64").copy()
        closes[-1] = price
        sess = sess.assign(close=closes)
        frames.append(sess)

    return pd.concat(frames, axis=0)


def make_multi_session(
    target_date: date_type,
    *,
    pattern: Pattern,
    trend: Literal["up", "down", "flat"],
    n_prior_sessions: int = 25,
    base_price: float = 100.0,
    daily_step: float = 0.2,
    symbol: str = "SPY",
    reclaim_start_bar: int = 60,
    extension_volume_decay: bool = False,
) -> pd.DataFrame:
    """Concatenate ``n_prior_sessions`` of trend-encoding prior sessions with
    a target session built by ``make_session``.

    The target session's ``base_price`` is set to the *last prior session's
    close*, so the intraday VWAP-relative pattern is unchanged but the daily
    trend regime evaluates as ``trend``.
    """
    if trend == "up":
        last_prior_price = base_price + daily_step * n_prior_sessions
    elif trend == "down":
        last_prior_price = base_price - daily_step * n_prior_sessions
    else:
        last_prior_price = base_price

    priors = make_prior_sessions(
        target_date,
        n_sessions=n_prior_sessions,
        trend=trend,
        base_price=base_price,
        daily_step=daily_step,
        symbol=symbol,
    )
    target = make_session(
        target_date,
        pattern=pattern,
        base_price=last_prior_price,
        reclaim_start_bar=reclaim_start_bar,
        symbol=symbol,
        extension_volume_decay=extension_volume_decay,
    )
    return pd.concat([priors, target], axis=0)
