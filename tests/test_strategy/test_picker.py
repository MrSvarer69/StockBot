from __future__ import annotations

import math

from trading_bot.strategy.picker import rank_entry_signals


def _entry(symbol: str, ratio: float, side: str = "long") -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "or_atr_ratio": ratio,
        "stop_price": 90.0,
        "take_price": 110.0,
        "target_size_pct": 0.1,
    }


def test_picks_top_n_by_ratio():
    sigs = {
        "A": _entry("A", 0.8),
        "B": _entry("B", 1.5),
        "C": _entry("C", 1.2),
        "D": _entry("D", 0.6),
    }
    picked = rank_entry_signals(sigs, max_picks=2)
    assert set(picked.keys()) == {"B", "C"}


def test_ties_broken_alphabetically():
    sigs = {
        "ZZZ": _entry("ZZZ", 1.0),
        "AAA": _entry("AAA", 1.0),
        "MMM": _entry("MMM", 1.0),
    }
    picked = rank_entry_signals(sigs, max_picks=2)
    assert set(picked.keys()) == {"AAA", "MMM"}


def test_picks_all_when_max_exceeds_count():
    sigs = {
        "A": _entry("A", 0.8),
        "B": _entry("B", 1.5),
    }
    picked = rank_entry_signals(sigs, max_picks=5)
    assert set(picked.keys()) == {"A", "B"}


def test_zero_max_returns_empty():
    sigs = {"A": _entry("A", 1.0)}
    assert rank_entry_signals(sigs, max_picks=0) == {}


def test_negative_max_returns_empty():
    sigs = {"A": _entry("A", 1.0)}
    assert rank_entry_signals(sigs, max_picks=-1) == {}


def test_empty_input_returns_empty():
    assert rank_entry_signals({}, max_picks=5) == {}


def test_nan_ratio_sorts_last():
    sigs = {
        "GOOD": _entry("GOOD", 0.5),
        "BAD": _entry("BAD", float("nan")),
    }
    picked = rank_entry_signals(sigs, max_picks=1)
    assert "GOOD" in picked
    assert "BAD" not in picked


def test_missing_ratio_key_treated_as_zero():
    """Defense in depth: signal missing the or_atr_ratio field sorts last."""
    sigs = {
        "GOOD": _entry("GOOD", 0.5),
        "INCOMPLETE": {
            "symbol": "INCOMPLETE",
            "side": "long",
            "stop_price": 90.0,
            "take_price": 110.0,
        },
    }
    picked = rank_entry_signals(sigs, max_picks=1)
    assert "GOOD" in picked


def test_min_ratio_excludes_weak_candidates():
    """Pool-entry floor: candidates below min_ratio are excluded before
    ranking. Defends a wide-universe deploy from trading the 'best of a
    bad lot' on slow days."""
    sigs = {
        "STRONG": _entry("STRONG", 1.8),
        "OK": _entry("OK", 1.2),
        "WEAK": _entry("WEAK", 0.6),
        "MARGINAL": _entry("MARGINAL", 0.55),
    }
    picked = rank_entry_signals(sigs, max_picks=5, min_ratio=1.0)
    assert set(picked.keys()) == {"STRONG", "OK"}


def test_min_ratio_zero_means_disabled():
    """min_ratio=None disables the floor (default behavior)."""
    sigs = {"A": _entry("A", 0.6), "B": _entry("B", 0.5)}
    picked = rank_entry_signals(sigs, max_picks=5, min_ratio=None)
    assert set(picked.keys()) == {"A", "B"}


def test_min_ratio_excludes_all_returns_empty():
    """If min_ratio rejects every candidate, return empty."""
    sigs = {"A": _entry("A", 0.6), "B": _entry("B", 0.7)}
    picked = rank_entry_signals(sigs, max_picks=5, min_ratio=1.0)
    assert picked == {}


def test_returned_signal_payload_is_unchanged():
    """The picker selects whole signal rows; it does not mutate them."""
    src = _entry("X", 1.0)
    picked = rank_entry_signals({"X": src}, max_picks=1)
    assert picked["X"] is src
    assert math.isclose(picked["X"]["or_atr_ratio"], 1.0)
