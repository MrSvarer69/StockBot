"""Pure ratchet math for trailing stops.

The bot uses a fixed-dollar trailing stop: the offset is captured once at
entry as `entry_price - initial_stop` (long) or `initial_stop - entry_price`
(short), and the stop is then ratcheted in the direction of profit but
never against the position. These helpers are pure — they take prices and
return a new stop level. No I/O, no broker calls.
"""

from __future__ import annotations

from decimal import Decimal


def trail_offset(entry_price: Decimal, initial_stop: Decimal, side: str) -> Decimal:
    """Compute the fixed-dollar trail offset from entry and initial stop.

    Returns a positive Decimal. Used once at entry; the trailing engine
    keeps this value constant for the life of the position.
    """
    if side == "long":
        return entry_price - initial_stop
    return initial_stop - entry_price


def ratchet_long_stop(
    current_stop: Decimal,
    highest_price: Decimal,
    offset: Decimal,
) -> Decimal:
    """New trailing stop for a long position. Never moves down."""
    candidate = highest_price - offset
    return candidate if candidate > current_stop else current_stop


def ratchet_short_stop(
    current_stop: Decimal,
    lowest_price: Decimal,
    offset: Decimal,
) -> Decimal:
    """New trailing stop for a short position. Never moves up."""
    candidate = lowest_price + offset
    return candidate if candidate < current_stop else current_stop


def ratchet_stop(
    *,
    side: str,
    current_stop: Decimal,
    extreme_price: Decimal,
    offset: Decimal,
) -> Decimal:
    """Dispatcher: pick the long or short ratchet based on side."""
    if side == "long":
        return ratchet_long_stop(current_stop, extreme_price, offset)
    return ratchet_short_stop(current_stop, extreme_price, offset)
