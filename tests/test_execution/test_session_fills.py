"""Fill-polling and partial-fill bookkeeping in the session loop.

Regression coverage for the 2026-05-27 bug where bracket market orders came
back PARTIALLY_FILLED (e.g. NSP ordered 60, filled 18) but the bot booked the
full ordered quantity. Root causes fixed here:

  • `_await_fill` stopped at the first partial fill (filled_avg_price became
    non-null), so the loop treated a partial as complete.
  • `_record_entry` + `cumulative_notional_today` used the ordered qty, not
    the filled qty, desyncing the book from the broker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pandas as pd

from trading_bot.contracts import OrderSide, OrderType, ProposedOrder, RiskParams
from trading_bot.execution.broker import BrokerError, BrokerOrderResponse
from trading_bot.execution.session import (
    SessionState,
    _await_fill,
    _handle_entry_signal,
    _is_fully_filled,
    _normalize_status,
    _record_entry,
)

from tests.test_execution.fakes import FakeBroker


def _resp(
    *,
    order_id: str = "ord-1",
    symbol: str = "SPY",
    qty: Decimal = Decimal("60"),
    status: str,
    filled_qty: Decimal,
    filled_avg_price: Decimal | None,
) -> BrokerOrderResponse:
    return BrokerOrderResponse(
        broker_order_id=order_id,
        client_order_id="cid-1",
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qty,
        status=status,
        submitted_at=datetime(2026, 5, 27, 14, 10, tzinfo=UTC),
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
    )


# --- status normalization helpers ------------------------------------------


def test_normalize_status_strips_enum_prefix():
    # Alpaca renders str(OrderStatus.PARTIALLY_FILLED) with the enum prefix.
    assert _normalize_status("OrderStatus.PARTIALLY_FILLED") == "partially_filled"
    assert _normalize_status("OrderStatus.FILLED") == "filled"
    # Plain strings (test fakes) pass through lowercased.
    assert _normalize_status("accepted") == "accepted"
    assert _normalize_status("filled") == "filled"


def test_is_fully_filled_recognizes_filled_status_and_full_qty():
    # Filled by status even if the enum-prefixed form is used.
    r = _resp(status="OrderStatus.FILLED", filled_qty=Decimal("60"), filled_avg_price=Decimal("33"))
    assert _is_fully_filled(r, Decimal("60"))
    # Filled by quantity reaching the ordered amount.
    r2 = _resp(status="accepted", filled_qty=Decimal("60"), filled_avg_price=Decimal("33"))
    assert _is_fully_filled(r2, Decimal("60"))
    # A partial (the bug): a fill price exists but qty < ordered → NOT full.
    r3 = _resp(status="OrderStatus.PARTIALLY_FILLED", filled_qty=Decimal("18"), filled_avg_price=Decimal("33.18"))
    assert not _is_fully_filled(r3, Decimal("60"))


# --- _await_fill polling ----------------------------------------------------


class _ScriptedBroker(FakeBroker):
    """Returns a scripted sequence of get_order responses (last one repeats)."""

    def __init__(self, sequence: list[BrokerOrderResponse], **kw):
        super().__init__(**kw)
        self._sequence = sequence
        self._calls = 0

    def get_order(self, broker_order_id: str) -> BrokerOrderResponse:
        i = min(self._calls, len(self._sequence) - 1)
        self._calls += 1
        return self._sequence[i]


def test_await_fill_polls_past_partial_until_full():
    """A first partial fill must NOT terminate the poll — keep going until the
    order is fully filled, then return the complete response."""
    ack = _resp(status="OrderStatus.PENDING_NEW", filled_qty=Decimal("0"), filled_avg_price=None)
    partial = _resp(status="OrderStatus.PARTIALLY_FILLED", filled_qty=Decimal("18"), filled_avg_price=Decimal("33.18"))
    full = _resp(status="OrderStatus.FILLED", filled_qty=Decimal("60"), filled_avg_price=Decimal("33.20"))
    broker = _ScriptedBroker([partial, full])

    out = _await_fill(broker, ack, sleep_fn=lambda _s: None)

    assert out.filled_qty == Decimal("60")
    assert _normalize_status(out.status) == "filled"


def test_await_fill_returns_stuck_partial_after_budget():
    """If the order never completes, `_await_fill` returns the latest partial
    after exhausting its attempt budget (it does not hang or fabricate a full
    fill)."""
    ack = _resp(status="OrderStatus.PENDING_NEW", filled_qty=Decimal("0"), filled_avg_price=None)
    partial = _resp(status="OrderStatus.PARTIALLY_FILLED", filled_qty=Decimal("18"), filled_avg_price=Decimal("33.18"))
    broker = _ScriptedBroker([partial])

    out = _await_fill(broker, ack, sleep_fn=lambda _s: None)

    assert out.filled_qty == Decimal("18")
    assert _normalize_status(out.status) == "partially_filled"


def test_await_fill_short_circuits_on_already_full_ack():
    """When the submit ack is already fully filled (the synchronous-fill fake),
    no polling happens."""
    ack = _resp(status="accepted", filled_qty=Decimal("60"), filled_avg_price=Decimal("33.18"))

    class _NoPoll(FakeBroker):
        def get_order(self, broker_order_id):  # pragma: no cover - must not run
            raise AssertionError("get_order should not be called")

    out = _await_fill(_NoPoll(), ack, sleep_fn=lambda _s: None)
    assert out is ack


# --- _record_entry books the filled qty ------------------------------------


def test_record_entry_books_filled_qty_not_ordered():
    """The open-entry record (and the qty that flows into the eventual Trade)
    must reflect the partial fill, not the larger ordered quantity."""
    sess = SessionState()
    sess.trailing_stop_policy = {"orb": True}
    order = ProposedOrder(
        symbol="NSP",
        side=OrderSide.BUY,
        qty=Decimal("60"),  # ordered
        order_type=OrderType.MARKET,
        stop_price=Decimal("31.88"),
        take_price=Decimal("35.51"),
        client_order_id="cid-1",
    )
    resp = _resp(
        symbol="NSP",
        qty=Decimal("60"),
        status="OrderStatus.PARTIALLY_FILLED",
        filled_qty=Decimal("18"),  # actually filled
        filled_avg_price=Decimal("33.18"),
    )

    _record_entry(sess, "NSP", "long", order, resp, strategy="orb")

    assert sess.open_entries["NSP"]["qty"] == Decimal("18")
    # Trail offset still computed from the fill price and the order's stop.
    assert sess.open_entries["NSP"]["trail_enabled"] is True


# --- _handle_entry_signal: notional + phantom guard ------------------------


def _entry_sig(symbol="SPY", target=Decimal("0.10")):
    return {
        "symbol": symbol,
        "side": "long",
        "target_size_pct": target,
        "stop_price": float("nan"),
        "take_price": float("nan"),
        "timestamp": pd.Timestamp("2026-05-27T14:10:00Z"),
        "strategy": "orb",
    }


def _risk():
    return RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
        max_position_count=5,
        symbol_whitelist=frozenset({"SPY"}),
    )


class _PartialFillBroker(FakeBroker):
    """Fills one share short of the ordered quantity and stays there."""

    def submit_order(self, order):
        self.submitted_orders.append(order)
        self._partial = _resp(
            symbol=order.symbol,
            qty=order.qty,
            status="OrderStatus.PARTIALLY_FILLED",
            filled_qty=order.qty - Decimal("1"),
            filled_avg_price=Decimal("100"),
        )
        return _resp(
            symbol=order.symbol,
            qty=order.qty,
            status="OrderStatus.PENDING_NEW",
            filled_qty=Decimal("0"),
            filled_avg_price=None,
        )

    def get_order(self, broker_order_id):
        return self._partial


def test_handle_entry_books_partial_fill_qty_and_notional():
    broker = _PartialFillBroker(latest_prices={"SPY": Decimal("100")})
    sess = SessionState()
    _handle_entry_signal(
        sig=_entry_sig(),
        broker=broker,
        risk_params=_risk(),
        sess=sess,
        sleep_fn=lambda _s: None,
    )

    ordered = broker.submitted_orders[0].qty
    booked = sess.open_entries["SPY"]["qty"]
    assert booked == ordered - Decimal("1")
    # Notional reflects the filled qty at the fill price, not the ordered qty.
    assert sess.cumulative_notional_today == booked * Decimal("100")


class _ZeroFillBroker(FakeBroker):
    """Order is acknowledged but never fills within the poll budget."""

    def submit_order(self, order):
        self.submitted_orders.append(order)
        self._unfilled = _resp(
            symbol=order.symbol,
            qty=order.qty,
            status="OrderStatus.PARTIALLY_FILLED",
            filled_qty=Decimal("0"),
            filled_avg_price=None,
        )
        return self._unfilled

    def get_order(self, broker_order_id):
        return self._unfilled


def test_handle_entry_skips_phantom_on_zero_fill():
    broker = _ZeroFillBroker(latest_prices={"SPY": Decimal("100")})
    sess = SessionState()
    _handle_entry_signal(
        sig=_entry_sig(),
        broker=broker,
        risk_params=_risk(),
        sess=sess,
        sleep_fn=lambda _s: None,
    )

    # No phantom position recorded and no notional charged.
    assert "SPY" not in sess.open_entries
    assert sess.cumulative_notional_today == Decimal("0")
