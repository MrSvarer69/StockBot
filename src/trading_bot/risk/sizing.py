"""Position sizing. All amounts are Decimal — never float for money."""

from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal

from ..contracts import RiskParams

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


def _to_decimal(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _zero(reason: str, **kv) -> Decimal:
    logger.warning("size_position returning 0: %s | %s", reason, kv)
    return _ZERO


def effective_equity(equity: Decimal, params: RiskParams) -> Decimal:
    """Equity actually used by the risk module's percentage caps.

    When ``params.max_capital_usd`` is set, returns ``min(equity, cap)`` so
    that sizing and daily caps act as if the operator only had the capped
    bankroll, even when the broker account holds more. Returns ``equity``
    unchanged when no cap is configured.
    """
    cap = params.max_capital_usd
    if cap is None:
        return equity
    return min(equity, cap)


def size_position(
    equity: Decimal,
    target_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal | None,
    params: RiskParams,
) -> Decimal:
    """Return whole-share quantity to trade.

    Caps applied (the smallest binds), where E is the capped equity (see
    ``effective_equity``):
      1. target notional   = E * target_pct
      2. per-trade ceiling = E * params.max_pct_per_trade
      3. risk-per-trade    = (E * params.max_pct_per_trade) / |entry - stop|

    Returns Decimal("0") on invalid inputs (non-positive equity/price, etc.)
    and logs a WARNING with the reason.
    """
    equity = _to_decimal(equity)
    target_pct = _to_decimal(target_pct)
    entry_price = _to_decimal(entry_price)

    if equity <= _ZERO:
        return _zero("non-positive equity", equity=equity)
    if entry_price <= _ZERO:
        return _zero("non-positive entry_price", entry_price=entry_price)
    if target_pct <= _ZERO:
        return _zero("non-positive target_pct", target_pct=target_pct)

    capped_equity = effective_equity(equity, params)
    if capped_equity <= _ZERO:
        return _zero("non-positive capped equity", capped_equity=capped_equity)

    target_notional = capped_equity * target_pct
    cap_notional = capped_equity * params.max_pct_per_trade
    notional = min(target_notional, cap_notional)

    qty = notional / entry_price

    if stop_price is not None:
        stop = _to_decimal(stop_price)
        if stop <= _ZERO:
            return _zero("non-positive stop_price", stop_price=stop)
        stop_distance = abs(entry_price - stop)
        if stop_distance > _ZERO:
            risk_dollars = capped_equity * params.max_pct_per_trade
            risk_qty = risk_dollars / stop_distance
            qty = min(qty, risk_qty)

    qty = qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
    if qty <= _ZERO:
        return _zero(
            "qty rounded to zero",
            target_notional=target_notional,
            cap_notional=cap_notional,
            entry_price=entry_price,
        )
    logger.debug(
        "size_position approved qty=%s equity=%s target_pct=%s entry=%s stop=%s",
        qty,
        equity,
        target_pct,
        entry_price,
        stop_price,
    )
    return qty
