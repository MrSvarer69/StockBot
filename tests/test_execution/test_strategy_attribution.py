"""End-to-end strategy attribution.

Pin the contract that every Trade and every `trade entry` / `trade exit`
log record carries the originating strategy name. This is the column
that downstream P&L attribution splits on, so a regression here would
silently break per-bot accounting.

The test drives `_record_entry` then `_record_exit` directly. That
mirrors what the live session loop does (see ``_handle_entry_signal``
and ``_check_stops_and_takes``) without re-running the full risk-sizing
+ submit pipeline.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.contracts import OrderSide, OrderType, ProposedOrder
from trading_bot.execution.broker import BrokerOrderResponse
from trading_bot.execution.session import (
    SessionState,
    _record_entry,
    _record_exit,
)


_ENTRY_TIME = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)
_EXIT_TIME = datetime(2026, 1, 5, 19, 45, tzinfo=UTC)


def _make_order(symbol: str = "SPY", qty: Decimal = Decimal("10")) -> ProposedOrder:
    return ProposedOrder(
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qty,
        order_type=OrderType.MARKET,
        stop_price=Decimal("95"),
        take_price=Decimal("110"),
        client_order_id=f"test-{uuid4()}",
    )


def _make_response(
    *,
    symbol: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    qty: Decimal = Decimal("10"),
    fill_price: Decimal = Decimal("100"),
    when: datetime = _ENTRY_TIME,
) -> BrokerOrderResponse:
    return BrokerOrderResponse(
        broker_order_id=str(uuid4()),
        client_order_id=f"test-{uuid4()}",
        symbol=symbol,
        side=side,
        qty=qty,
        status="filled",
        submitted_at=when,
        filled_qty=qty,
        filled_avg_price=fill_price,
    )


@pytest.mark.parametrize("strategy", ["orb", "pullback", "insider", ""])
def test_strategy_attribution_round_trip(strategy, caplog):
    """A strategy label set at entry must thread through to:

    1. ``sess.open_entries[symbol]["strategy"]``
    2. ``sess.trades[-1].strategy``
    3. The ``trade entry`` info log's ``extra`` dict
    4. The ``trade exit`` info log's ``extra`` dict

    Parametrized over each live strategy name plus the empty default
    that older callers (e.g. legacy backtest fixtures) still rely on.
    """
    sess = SessionState()
    symbol = "SPY"
    order = _make_order(symbol=symbol)
    entry_resp = _make_response(
        symbol=symbol, side=OrderSide.BUY, qty=order.qty,
        fill_price=Decimal("100"), when=_ENTRY_TIME,
    )

    with caplog.at_level(logging.INFO, logger="trading_bot.execution.session"):
        _record_entry(sess, symbol, "long", order, entry_resp, strategy=strategy)

        # Assertion 1: open_entries carries the strategy label.
        assert sess.open_entries[symbol]["strategy"] == strategy

        # Assertion 3: the "trade entry" log extra contains strategy.
        entry_records = [
            r for r in caplog.records if r.message == "trade entry"
        ]
        assert len(entry_records) == 1, (
            "expected exactly one 'trade entry' log record, got "
            f"{[r.message for r in caplog.records]}"
        )
        assert entry_records[0].strategy == strategy

        # Now drive the exit.
        exit_resp = _make_response(
            symbol=symbol, side=OrderSide.SELL, qty=order.qty,
            fill_price=Decimal("105"), when=_EXIT_TIME,
        )
        _record_exit(sess, symbol, exit_resp, exit_reason="take")

        # Assertion 2: the Trade record carries the strategy label.
        assert len(sess.trades) == 1
        assert sess.trades[-1].strategy == strategy
        # `open_entries` is popped on exit; sanity-check the bookkeeping.
        assert symbol not in sess.open_entries

        # Assertion 4: the "trade exit" log extra contains strategy.
        exit_records = [
            r for r in caplog.records if r.message == "trade exit"
        ]
        assert len(exit_records) == 1, (
            "expected exactly one 'trade exit' log record, got "
            f"{[r.message for r in caplog.records]}"
        )
        assert exit_records[0].strategy == strategy
