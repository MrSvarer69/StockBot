"""Pre-trade order validation. Every order placement path must go through here."""

from __future__ import annotations

import logging
from decimal import Decimal

from ..contracts import AccountState, OrderDecision, ProposedOrder, RiskParams
from .kill_switch import kill_switches_ok

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


def _reject(reason: str, order: ProposedOrder) -> OrderDecision:
    logger.warning(
        "order rejected: %s | symbol=%s side=%s qty=%s",
        reason,
        order.symbol,
        order.side,
        order.qty,
    )
    return OrderDecision(approved=False, reason=reason, order=None)


def validate_order(
    order: ProposedOrder,
    state: AccountState,
    params: RiskParams,
    *,
    market_is_open: bool,
    current_price: Decimal | None = None,
) -> OrderDecision:
    """Sequential checks. Returns the first failure or approve.

    Order of checks: connection first (everything else assumes the broker view
    is meaningful), then per-order sanity, then state caps, then kill switches.

    `current_price` is required to enforce the daily total-notional cap on
    market orders. Without it, the cap check is skipped and a warning is logged.
    """
    if not state.connection_ok:
        return _reject("connection down", order)

    if order.qty <= _ZERO:
        return _reject("non-positive qty", order)

    if not order.qty.is_finite():
        return _reject("non-finite qty", order)

    for label, value in (
        ("limit_price", order.limit_price),
        ("stop_price", order.stop_price),
        ("take_price", order.take_price),
    ):
        if value is not None and value <= _ZERO:
            return _reject(f"non-positive {label}", order)

    if params.symbol_whitelist and order.symbol not in params.symbol_whitelist:
        return _reject(f"symbol {order.symbol} not in whitelist", order)

    if state.open_order_count >= params.max_open_orders:
        return _reject(
            f"open orders {state.open_order_count} >= cap {params.max_open_orders}",
            order,
        )

    if order.symbol not in state.open_positions:
        if len(state.open_positions) >= params.max_position_count:
            return _reject(
                f"position count {len(state.open_positions)} >= cap "
                f"{params.max_position_count}",
                order,
            )

    if not market_is_open:
        return _reject("market closed", order)

    px = current_price if current_price is not None else order.limit_price
    if px is None:
        logger.warning(
            "daily notional cap skipped: no current_price or limit_price for %s",
            order.symbol,
        )
    else:
        from .sizing import effective_equity

        proposed_notional = abs(order.qty) * px

        # Hard total-exposure ceiling. When max_capital_usd is set it is an
        # absolute dollar cap on capital deployed at once: block this entry if
        # the live open-position exposure plus the proposed order would exceed
        # it. This is distinct from the percentage caps (which only *scale*
        # their base by effective_equity) — those bound each trade and the
        # daily flow, but nothing previously bounded total simultaneous
        # holdings. Existing exposure is measured at cost basis
        # (|qty| * avg_entry_price); the new order at current price. The
        # order's own symbol is excluded from the existing sum so the (rare)
        # add-to-position path is not double-counted — entries are skipped
        # upstream when a position is already open, so in practice this only
        # ever sums *other* symbols.
        if params.max_capital_usd is not None:
            existing_exposure = sum(
                (
                    abs(p.qty) * p.avg_entry_price
                    for sym, p in state.open_positions.items()
                    if sym != order.symbol
                ),
                _ZERO,
            )
            if existing_exposure + proposed_notional > params.max_capital_usd:
                return _reject(
                    f"capital cap: open exposure {existing_exposure} + order "
                    f"{proposed_notional} > cap {params.max_capital_usd}",
                    order,
                )

        daily_cap = params.max_daily_notional_pct * effective_equity(state.equity, params)
        if state.cumulative_notional_today + proposed_notional > daily_cap:
            return _reject(
                f"daily notional cap hit: {state.cumulative_notional_today} + "
                f"{proposed_notional} > {daily_cap}",
                order,
            )

    ok, reason = kill_switches_ok(state, params)
    if not ok:
        return _reject(f"kill switch tripped: {reason}", order)

    logger.info(
        "order approved: symbol=%s side=%s qty=%s type=%s",
        order.symbol,
        order.side,
        order.qty,
        order.order_type,
    )
    return OrderDecision(approved=True, reason="ok", order=order)
