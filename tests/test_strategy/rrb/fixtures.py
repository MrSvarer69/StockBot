"""Synthetic 1-min bars for RRB strategy tests.

The fixtures build sessions whose rolling N-min high/low channel can be
reasoned about explicitly. Construction:

  - The first ``lookback_minutes`` of the session oscillate tightly inside
    a narrow band around ``base_price``, establishing a well-defined
    rolling reference channel.
  - After the warm-up window, a deterministic breakout (or no breakout,
    or a mirrored breakdown) is injected at a known offset.

This lets tests assert: signal fires at the expected bar, score equals a
predictable value, and -- the load-bearing claim -- breakouts that close
deeper through the reference produce strictly higher scores.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

Pattern = Literal[
    "breakout_up",      # warm-up oscillates, then a sustained move above the band
    "breakdown_down",   # warm-up oscillates, then a sustained move below
    "flat",             # closes hug base_price; no breakout
    "shallow_poke",     # one-bar excursion above the band, then back inside
]


def make_session(
    session_date: date_type,
    *,
    pattern: Pattern = "breakout_up",
    base_price: float = 100.0,
    band_half_width: float = 0.05,
    breakout_size: float = 0.5,
    breakout_start_bar: int = 75,
    symbol: str = "SPY",
    warmup_bars: int = 60,
) -> pd.DataFrame:
    """Build a single 09:30-16:00 ET session of 1-min UTC bars.

    The first ``warmup_bars`` oscillate tightly around ``base_price`` within
    ``+/-band_half_width``, so the rolling-N-min high after warm-up sits at
    ``base_price + band_half_width`` and the rolling low at
    ``base_price - band_half_width``.

    From ``breakout_start_bar`` onward, the pattern injects a deterministic
    move:

      - ``breakout_up``: closes step to ``base_price + breakout_size`` (which
        is far above the band's rolling high) and stay there.
      - ``breakdown_down``: mirrored.
      - ``flat``: continues oscillating; no breakout.
      - ``shallow_poke``: one bar at ``base_price + breakout_size`` then
        back inside the band on subsequent bars.

    ``breakout_size`` is the *price* move above/below the base. The bar
    high/low envelope around each close is +/- 0.05, so the rolling-high
    measured over the warm-up window is approximately
    ``base_price + band_half_width + 0.05``.

    ``warmup_bars`` should be at least the strategy's ``lookback_minutes``
    so the rolling window is fully populated before the breakout fires.
    """
    start = pd.Timestamp.combine(session_date, pd.Timestamp("09:30").time())
    start = start.tz_localize(ET).tz_convert("UTC")
    n = 6 * 60 + 30  # 390 minutes
    idx = pd.DatetimeIndex(
        pd.date_range(start, periods=n, freq="1min", tz="UTC"), name="timestamp"
    )

    closes: list[float] = []
    for i in range(n):
        if i < breakout_start_bar:
            sign = 1 if (i % 2 == 0) else -1
            closes.append(base_price + sign * band_half_width)
            continue

        if pattern == "breakout_up":
            closes.append(base_price + breakout_size)
        elif pattern == "breakdown_down":
            closes.append(base_price - breakout_size)
        elif pattern == "flat":
            sign = 1 if (i % 2 == 0) else -1
            closes.append(base_price + sign * band_half_width)
        elif pattern == "shallow_poke":
            if i == breakout_start_bar:
                closes.append(base_price + breakout_size)
            else:
                sign = 1 if (i % 2 == 0) else -1
                closes.append(base_price + sign * band_half_width)
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
            "volume": [1000.0] * n,
            "symbol": [symbol] * n,
        },
        index=idx,
    )
