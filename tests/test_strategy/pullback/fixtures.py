"""Synthetic 1-min bar fixtures for the PullbackStrategy tests.

The fixtures construct a single session whose close series traces a
three-phase shape: a trending warm-up that lifts EMA_fast above EMA_slow
(or below for shorts), a pullback dipping through EMA_fast for a few bars,
then a reclaim back above EMA_fast. Tests assert the strategy fires only
when all three phases are present.

All times in UTC bars; ET conversion happens inside the strategy.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

import pandas as pd

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

Phase = Literal["trend_up_pullback_reclaim", "trend_up_no_pullback",
                "trend_down_pullback_reclaim", "flat_no_trend"]


def make_session(
    session_date: date_type,
    *,
    phase: Phase = "trend_up_pullback_reclaim",
    base_price: float = 100.0,
    symbol: str = "SPY",
    pullback_start_bar: int = 150,
    pullback_bars: int = 6,
    pullback_depth: float = 1.2,
    trend_slope: float = 0.04,
) -> pd.DataFrame:
    """Build a 390-bar 09:30-16:00 ET session in UTC 1-min bars.

    The construction phases:

      * Bars [0, pullback_start_bar): a uniform ramp at ``trend_slope`` per
        bar. With a 50-bar EMA, ~80 bars suffice to lift EMA_fast clearly
        above EMA_slow (or below if slope < 0).

      * Bars [pullback_start_bar, pullback_start_bar + pullback_bars):
        the close drops to ``base_price + ramp_value - pullback_depth``
        (or rises by that much for shorts), pulling below EMA_fast.

      * Remaining bars: ramp resumes from the pullback exit price -- the
        reclaim is the first bar after the pullback.
    """
    start = pd.Timestamp.combine(session_date, pd.Timestamp("09:30").time())
    start = start.tz_localize(ET).tz_convert("UTC")
    n = 6 * 60 + 30
    idx = pd.DatetimeIndex(
        pd.date_range(start, periods=n, freq="1min", tz="UTC"), name="timestamp",
    )

    if phase == "flat_no_trend":
        closes = [base_price + ((i % 2) * 0.02 - 0.01) for i in range(n)]
    else:
        sign = 1 if "down" not in phase else -1
        closes = []
        ramp = 0.0
        for i in range(n):
            if i < pullback_start_bar:
                ramp = i * trend_slope * sign
                closes.append(base_price + ramp)
                continue
            if phase in (
                "trend_up_pullback_reclaim",
                "trend_down_pullback_reclaim",
            ):
                if i < pullback_start_bar + pullback_bars:
                    # Pullback runs against the trend.
                    closes.append(
                        base_price
                        + pullback_start_bar * trend_slope * sign
                        - sign * pullback_depth
                    )
                    continue
            # Resume ramp from the post-pullback bar; recompute ramp value
            # so the trend stays intact.
            ramp = i * trend_slope * sign
            closes.append(base_price + ramp)

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
