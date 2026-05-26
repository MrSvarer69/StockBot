"""Strategy protocol and helpers. Strategies are pure functions over BarsFrames."""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class Strategy(Protocol):
    """A strategy converts bars into a SignalsFrame (see contracts.py)."""

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame: ...


def empty_signals_frame() -> pd.DataFrame:
    """Return a correctly-typed empty SignalsFrame.

    ``score`` is the unified, strategy-agnostic conviction column used by a
    cross-strategy picker. ``or_atr_ratio`` is retained for backward
    compatibility with ``strategy/picker.py`` until that migration is run;
    every strategy emits both columns with the same numeric value.

    ``strategy`` labels each row with the producer ("orb", "pullback",
    "insider") so downstream attribution can split P&L by source. The
    column appears on entry rows AND flat rows.
    """
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "symbol": pd.Series(dtype="object"),
            "side": pd.Series(dtype="object"),
            "strategy": pd.Series(dtype="object"),
            "target_size_pct": pd.Series(dtype="float64"),
            "stop_price": pd.Series(dtype="float64"),
            "take_price": pd.Series(dtype="float64"),
            "score": pd.Series(dtype="float64"),
            "or_atr_ratio": pd.Series(dtype="float64"),
        }
    )
