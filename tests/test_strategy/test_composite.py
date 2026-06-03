"""Unit tests for CompositeStrategy.

Pin behavior on small in-memory stub strategies. These tests do not call
any real strategy (no parquet, no bars synthesis) — they verify the merge
+ conflict-resolution logic in isolation.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategy.base import empty_signals_frame
from trading_bot.strategy.composite import CompositeStrategy


def _entry(
    ts: str,
    symbol: str,
    *,
    side: str = "long",
    score: float = 1.0,
    stop: float = 99.0,
    take: float = 102.0,
    strategy: str = "orb",
) -> dict:
    return {
        "timestamp": pd.Timestamp(ts, tz="UTC"),
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "target_size_pct": 0.10,
        "stop_price": stop,
        "take_price": take,
        "entry_price": 100.0,
        "score": score,
        "or_atr_ratio": score,
    }


def _flat(ts: str, symbol: str, *, strategy: str = "orb") -> dict:
    return {
        "timestamp": pd.Timestamp(ts, tz="UTC"),
        "symbol": symbol,
        "side": "flat",
        "strategy": strategy,
        "target_size_pct": 0.10,
        "stop_price": float("nan"),
        "take_price": float("nan"),
        "entry_price": float("nan"),
        "score": float("nan"),
        "or_atr_ratio": float("nan"),
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return empty_signals_frame()
    out = pd.DataFrame(rows)
    return out[
        [
            "timestamp",
            "symbol",
            "side",
            "strategy",
            "target_size_pct",
            "stop_price",
            "take_price",
            "entry_price",
            "score",
            "or_atr_ratio",
        ]
    ]


class _StubStrategy:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        return self._frame


def _dummy_bars() -> pd.DataFrame:
    # The inner strategies are stubs; they ignore bars. We pass a minimal
    # frame so CompositeStrategy has something to forward.
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_empty_strategy_list_rejected():
    with pytest.raises(ValueError):
        CompositeStrategy(strategies=[])


def test_names_length_mismatch_rejected():
    s = _StubStrategy(empty_signals_frame())
    with pytest.raises(ValueError):
        CompositeStrategy(strategies=[s], names=["a", "b"])


# ---------------------------------------------------------------------------
# Merge / shape
# ---------------------------------------------------------------------------


def test_non_overlapping_signals_concat_correctly():
    a = _StubStrategy(_frame([_entry("2026-01-05 14:30", "AAA", score=1.2)]))
    b = _StubStrategy(_frame([_entry("2026-01-05 14:45", "BBB", score=0.8)]))
    composite = CompositeStrategy([a, b])

    out = composite.generate_signals(_dummy_bars())
    assert len(out) == 2
    assert set(out["symbol"]) == {"AAA", "BBB"}
    # Sorted by timestamp
    assert list(out["symbol"]) == ["AAA", "BBB"]


def test_output_schema_matches_empty_signals_frame():
    a = _StubStrategy(_frame([_entry("2026-01-05 14:30", "AAA")]))
    composite = CompositeStrategy([a])

    out = composite.generate_signals(_dummy_bars())
    expected_cols = list(empty_signals_frame().columns)
    assert list(out.columns) == expected_cols


def test_both_empty_returns_empty_signals_frame():
    a = _StubStrategy(empty_signals_frame())
    b = _StubStrategy(empty_signals_frame())
    composite = CompositeStrategy([a, b])

    out = composite.generate_signals(_dummy_bars())
    assert out.empty
    assert list(out.columns) == list(empty_signals_frame().columns)


def test_one_empty_other_passes_through_unchanged():
    a = _StubStrategy(empty_signals_frame())
    b = _StubStrategy(_frame([_entry("2026-01-05 14:30", "BBB", score=1.0)]))
    composite = CompositeStrategy([a, b])

    out = composite.generate_signals(_dummy_bars())
    assert len(out) == 1
    assert out.iloc[0]["symbol"] == "BBB"
    assert out.iloc[0]["score"] == 1.0


# ---------------------------------------------------------------------------
# Entry conflicts
# ---------------------------------------------------------------------------


def test_conflicting_entries_higher_score_wins():
    # Same ts, same symbol, opposing sides. Higher score (long, 1.5) wins
    # over short (0.8).
    a = _StubStrategy(
        _frame([_entry("2026-01-05 14:30", "AAA", side="long", score=1.5)])
    )
    b = _StubStrategy(
        _frame([_entry("2026-01-05 14:30", "AAA", side="short", score=0.8)])
    )
    composite = CompositeStrategy([a, b])

    out = composite.generate_signals(_dummy_bars())
    assert len(out) == 1
    assert out.iloc[0]["side"] == "long"
    assert out.iloc[0]["score"] == 1.5


def test_conflicting_entries_tie_broken_by_priority_order():
    # Same ts, same symbol, equal score. First-listed strategy wins.
    a = _StubStrategy(
        _frame(
            [
                _entry(
                    "2026-01-05 14:30",
                    "AAA",
                    side="long",
                    score=1.0,
                    stop=99.0,
                    take=103.0,
                )
            ]
        )
    )
    b = _StubStrategy(
        _frame(
            [
                _entry(
                    "2026-01-05 14:30",
                    "AAA",
                    side="short",
                    score=1.0,
                    stop=101.0,
                    take=97.0,
                )
            ]
        )
    )
    composite = CompositeStrategy([a, b], names=["alpha", "beta"])

    out = composite.generate_signals(_dummy_bars())
    assert len(out) == 1
    assert out.iloc[0]["side"] == "long"  # alpha won the tie
    assert out.iloc[0]["take_price"] == 103.0


def test_conflict_only_applies_same_timestamp():
    # Same symbol but different timestamps — both should pass through.
    a = _StubStrategy(
        _frame([_entry("2026-01-05 14:30", "AAA", side="long", score=1.5)])
    )
    b = _StubStrategy(
        _frame([_entry("2026-01-05 14:45", "AAA", side="short", score=0.8)])
    )
    composite = CompositeStrategy([a, b])

    out = composite.generate_signals(_dummy_bars())
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Flats are not deduplicated
# ---------------------------------------------------------------------------


def test_flat_signals_from_multiple_strategies_all_pass_through():
    # Two strategies both want to flatten AAA at the same time. Both rows
    # must survive; the session loop is responsible for idempotency.
    a = _StubStrategy(_frame([_flat("2026-01-05 15:55", "AAA")]))
    b = _StubStrategy(_frame([_flat("2026-01-05 15:55", "AAA")]))
    composite = CompositeStrategy([a, b])

    out = composite.generate_signals(_dummy_bars())
    flats = out[out["side"] == "flat"]
    assert len(flats) == 2


def test_entry_and_flat_at_same_bar_both_kept():
    # A flat from one strategy and an entry from another at the same
    # (ts, symbol) — they describe different actions and both must survive.
    a = _StubStrategy(_frame([_entry("2026-01-05 15:55", "AAA", score=1.0)]))
    b = _StubStrategy(_frame([_flat("2026-01-05 15:55", "AAA")]))
    composite = CompositeStrategy([a, b])

    out = composite.generate_signals(_dummy_bars())
    assert len(out) == 2
    assert set(out["side"]) == {"long", "flat"}
