"""Synthetic 1-min bars for ORB strategy tests."""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

Breakout = Literal["up", "down", "none"]


def make_synthetic_session(
    session_date: date_type,
    *,
    breakout: Breakout = "none",
    flat_range: bool = False,
    or_high: float = 101.0,
    or_low: float = 99.0,
    symbol: str = "SPY",
) -> pd.DataFrame:
    """Return one ET trading session (09:30–16:00) of 1-min bars in UTC.

    During the first 30 min, prices oscillate between or_low and or_high.
    After 10:00 ET, prices stay inside the range (no breakout), break above
    or_high (up), or below or_low (down).

    flat_range=True forces a very tight OR (so min_range_atr_multiplier filters it).
    """
    start = pd.Timestamp.combine(session_date, pd.Timestamp("09:30").time())
    start = start.tz_localize(ET).tz_convert("UTC")
    n = 6 * 60 + 30  # 09:30 → 16:00 = 390 minutes
    idx = pd.DatetimeIndex(
        pd.date_range(start, periods=n, freq="1min", tz="UTC"), name="timestamp"
    )

    if flat_range:
        or_low, or_high = 100.0, 100.05  # ~5 cent range

    mid = (or_high + or_low) / 2
    half = (or_high - or_low) / 2

    closes: list[float] = []
    for i in range(n):
        if i < 30:
            # First 30 min: oscillate between or_low and or_high
            sign = 1 if (i % 2 == 0) else -1
            closes.append(mid + sign * half * 0.9)
        else:
            if breakout == "up":
                closes.append(or_high + 0.5 + (i - 30) * 0.001)
            elif breakout == "down":
                closes.append(or_low - 0.5 - (i - 30) * 0.001)
            else:
                sign = 1 if (i % 2 == 0) else -1
                closes.append(mid + sign * half * 0.5)

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.05 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.05 for o, c in zip(opens, closes)]

    # Force OR_high / OR_low to be exactly the configured values.
    for i in range(30):
        if i == 0:
            highs[i] = or_high
        elif i == 1:
            lows[i] = or_low

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * n,
            "symbol": [symbol] * n,
        },
        index=idx,
    )


def make_multi_session(sessions: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(sessions).sort_index()
