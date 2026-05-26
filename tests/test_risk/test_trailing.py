"""Pure-function tests for the trailing-stop ratchet math.

The trailing engine has exactly one safety invariant that the
risk-officer review flagged as load-bearing: on the first poll
after entry, with the most recent extreme equal to the entry price,
the ratchet MUST return the original stop unchanged. A regression
that ratchets above the initial stop on poll one would tighten the
stop into the noise band the strategy explicitly chose to give the
trade. This module pins that invariant alongside the basic monotonicity
contracts.

Decimal throughout — no float arithmetic. The production code uses
Decimal exclusively (see `trading_bot.risk.trailing`), so introducing
float here would silently mask quantization drift the real engine
cannot have.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.risk.trailing import (
    ratchet_long_stop,
    ratchet_short_stop,
    ratchet_stop,
    trail_offset,
)


# --- trail_offset: produces a positive Decimal for both sides --------------


@pytest.mark.parametrize(
    "entry, initial_stop, expected",
    [
        (Decimal("100"), Decimal("95"), Decimal("5")),
        (Decimal("250.50"), Decimal("248.00"), Decimal("2.50")),
        (Decimal("12.34"), Decimal("12.00"), Decimal("0.34")),
    ],
)
def test_trail_offset_long(entry, initial_stop, expected):
    offset = trail_offset(entry, initial_stop, "long")
    assert offset == expected
    assert offset > 0
    assert isinstance(offset, Decimal)


@pytest.mark.parametrize(
    "entry, initial_stop, expected",
    [
        (Decimal("100"), Decimal("105"), Decimal("5")),
        (Decimal("30.00"), Decimal("31.25"), Decimal("1.25")),
        (Decimal("742.10"), Decimal("750.00"), Decimal("7.90")),
    ],
)
def test_trail_offset_short(entry, initial_stop, expected):
    offset = trail_offset(entry, initial_stop, "short")
    assert offset == expected
    assert offset > 0
    assert isinstance(offset, Decimal)


# --- ratchet_long_stop monotonicity ----------------------------------------


def test_ratchet_long_stop_no_op_when_candidate_below_current():
    # highest_price - offset = 100 - 5 = 95, which equals current_stop.
    # Strict-greater check means equal is treated as no-op.
    result = ratchet_long_stop(
        current_stop=Decimal("95"),
        highest_price=Decimal("100"),
        offset=Decimal("5"),
    )
    assert result == Decimal("95")


def test_ratchet_long_stop_no_op_when_price_drops_back():
    # current already advanced to 98, then price dips — stop must NOT move down.
    result = ratchet_long_stop(
        current_stop=Decimal("98"),
        highest_price=Decimal("101"),
        offset=Decimal("5"),
    )
    # candidate = 96, current is 98 — keep 98.
    assert result == Decimal("98")


def test_ratchet_long_stop_tightens_when_price_advances():
    # entry 100, initial stop 95 (offset 5). Price moves to 110.
    # New stop should be 105.
    result = ratchet_long_stop(
        current_stop=Decimal("95"),
        highest_price=Decimal("110"),
        offset=Decimal("5"),
    )
    assert result == Decimal("105")


def test_ratchet_long_stop_idempotent_called_twice():
    once = ratchet_long_stop(
        current_stop=Decimal("95"),
        highest_price=Decimal("108"),
        offset=Decimal("5"),
    )
    twice = ratchet_long_stop(
        current_stop=once,
        highest_price=Decimal("108"),
        offset=Decimal("5"),
    )
    # First call moves stop to 103; second call with the same high should
    # not move it again (candidate == current — strict-greater rejects).
    assert once == Decimal("103")
    assert twice == once


# --- ratchet_short_stop monotonicity --------------------------------------


def test_ratchet_short_stop_no_op_when_candidate_above_current():
    # lowest_price + offset = 30 + 1 = 31 == current_stop.
    result = ratchet_short_stop(
        current_stop=Decimal("31"),
        lowest_price=Decimal("30"),
        offset=Decimal("1"),
    )
    assert result == Decimal("31")


def test_ratchet_short_stop_no_op_when_price_rallies_back():
    # current already tightened to 29, then price rallies — stop must NOT move up.
    result = ratchet_short_stop(
        current_stop=Decimal("29"),
        lowest_price=Decimal("29.50"),
        offset=Decimal("1"),
    )
    # candidate = 30.50, current is 29 — keep 29.
    assert result == Decimal("29")


def test_ratchet_short_stop_tightens_when_price_drops():
    # entry 30, initial stop 31 (offset 1). Price drops to 25.
    # New stop should be 26.
    result = ratchet_short_stop(
        current_stop=Decimal("31"),
        lowest_price=Decimal("25"),
        offset=Decimal("1"),
    )
    assert result == Decimal("26")


def test_ratchet_short_stop_idempotent_called_twice():
    once = ratchet_short_stop(
        current_stop=Decimal("31"),
        lowest_price=Decimal("28"),
        offset=Decimal("1"),
    )
    twice = ratchet_short_stop(
        current_stop=once,
        lowest_price=Decimal("28"),
        offset=Decimal("1"),
    )
    assert once == Decimal("29")
    assert twice == once


# --- ratchet_stop dispatcher selects the correct helper -------------------


def test_ratchet_stop_dispatches_long():
    result = ratchet_stop(
        side="long",
        current_stop=Decimal("95"),
        extreme_price=Decimal("110"),
        offset=Decimal("5"),
    )
    assert result == Decimal("105")


def test_ratchet_stop_dispatches_short():
    result = ratchet_stop(
        side="short",
        current_stop=Decimal("31"),
        extreme_price=Decimal("25"),
        offset=Decimal("1"),
    )
    assert result == Decimal("26")


def test_ratchet_stop_long_respects_monotonicity_via_dispatcher():
    # Same monotonicity check, just through the dispatcher.
    result = ratchet_stop(
        side="long",
        current_stop=Decimal("105"),
        extreme_price=Decimal("108"),
        offset=Decimal("5"),
    )
    # candidate = 103, current = 105 — keep 105.
    assert result == Decimal("105")


# --- Safety invariant: first poll after entry must be a no-op -------------


def test_ratchet_long_no_op_on_first_poll_after_entry():
    """Risk-officer invariant. On poll one, before price has moved, the
    extreme equals entry_price and the offset equals (entry_price -
    initial_stop). The ratchet candidate is exactly initial_stop and MUST
    NOT be considered a tightening — otherwise we'd start moving the stop
    on every flat tick, eating the strategy's noise band."""
    entry_price = Decimal("100")
    initial_stop = Decimal("95")
    offset = trail_offset(entry_price, initial_stop, "long")
    result = ratchet_long_stop(
        current_stop=initial_stop,
        highest_price=entry_price,
        offset=offset,
    )
    assert result == initial_stop


def test_ratchet_short_no_op_on_first_poll_after_entry():
    """Mirror of the long invariant for short positions."""
    entry_price = Decimal("30")
    initial_stop = Decimal("31")
    offset = trail_offset(entry_price, initial_stop, "short")
    result = ratchet_short_stop(
        current_stop=initial_stop,
        lowest_price=entry_price,
        offset=offset,
    )
    assert result == initial_stop


def test_ratchet_stop_dispatcher_no_op_on_first_poll_both_sides():
    """Same invariant routed through the public dispatcher entrypoint."""
    # Long
    entry, stop = Decimal("100"), Decimal("95")
    offset = trail_offset(entry, stop, "long")
    assert (
        ratchet_stop(
            side="long",
            current_stop=stop,
            extreme_price=entry,
            offset=offset,
        )
        == stop
    )
    # Short
    entry, stop = Decimal("30"), Decimal("31")
    offset = trail_offset(entry, stop, "short")
    assert (
        ratchet_stop(
            side="short",
            current_stop=stop,
            extreme_price=entry,
            offset=offset,
        )
        == stop
    )
