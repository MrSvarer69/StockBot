"""Regression tests for the Alpaca paper broker adapter.

Focus: price-quantization at the wire boundary. Strategy math is float-based
and emits sub-penny dust (e.g. take_price=744.3450000000003). Alpaca rejects
such payloads with API code 42210000. The broker adapter must quantize to the
allowed tick before submitting; these tests pin that contract.

Also covers `replace_stop_price` — the trailing-stop adapter the session loop
calls to ratchet the broker-side stop leg without the cancel+place race.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from trading_bot.contracts import OrderSide, OrderType, ProposedOrder
from trading_bot.execution.alpaca_paper import (
    AlpacaPaperBroker,
    _quantize_to_tick,
)
from trading_bot.execution.broker import (
    BrokerAuthError,
    BrokerError,
    BrokerValidationError,
)


# --- Pure-function tests for the quantizer ---------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The exact value from the 2026-05-22 production halt.
        (Decimal("744.3450000000003"), Decimal("744.35")),
        # Already on-tick — no change.
        (Decimal("100.00"), Decimal("100.00")),
        (Decimal("100.01"), Decimal("100.01")),
        # Half-up rounding for the boundary.
        (Decimal("100.005"), Decimal("100.01")),
        (Decimal("100.004"), Decimal("100.00")),
        # Penny tick applies down to exactly $1.00.
        (Decimal("1.00"), Decimal("1.00")),
        (Decimal("1.005"), Decimal("1.01")),
        # Sub-penny tick for prices below $1.00.
        (Decimal("0.99"), Decimal("0.9900")),
        (Decimal("0.12345"), Decimal("0.1235")),
        (Decimal("0.00005"), Decimal("0.0001")),
    ],
)
def test_quantize_to_tick(raw: Decimal, expected: Decimal) -> None:
    assert _quantize_to_tick(raw) == expected


def test_quantize_to_tick_passes_through_non_positive() -> None:
    # Non-positive prices are a bug upstream; quantization must NOT mask them
    # by silently producing 0 (which Alpaca might accept as "no take").
    assert _quantize_to_tick(Decimal("0")) == Decimal("0")
    assert _quantize_to_tick(Decimal("-1.234")) == Decimal("-1.234")


# --- Integration test: submit_order quantizes before the SDK call ----------


def _make_bracket_order(
    *,
    stop: Decimal,
    take: Decimal,
    symbol: str = "CRWD",
) -> ProposedOrder:
    return ProposedOrder(
        symbol=symbol,
        side=OrderSide.BUY,
        qty=Decimal("2"),
        order_type=OrderType.MARKET,
        stop_price=stop,
        take_price=take,
        client_order_id=f"test-{symbol}",
    )


def _fake_trading_client_response(symbol: str) -> MagicMock:
    """Return a mocked TradingClient that records `submit_order` calls and
    returns a minimal response object the adapter can wrap."""
    resp = MagicMock()
    resp.id = "broker-id-1"
    resp.client_order_id = f"test-{symbol}"
    resp.status = "accepted"
    resp.submitted_at = datetime(2026, 5, 22, 14, 1, 54, tzinfo=UTC)
    resp.filled_qty = "0"
    resp.filled_avg_price = None

    client = MagicMock()
    client.submit_order.return_value = resp
    return client


def test_submit_order_quantizes_bracket_prices_against_regression() -> None:
    """The exact 2026-05-22 production payload must reach the SDK on-tick."""
    broker = AlpacaPaperBroker(api_key="dummy", api_secret="dummy")
    fake_client = _fake_trading_client_response("CRWD")
    broker._trading = fake_client  # type: ignore[assignment]

    order = _make_bracket_order(
        stop=Decimal("635.1074999999998"),
        take=Decimal("744.3450000000003"),
    )
    broker.submit_order(order)

    # Pull the request object out of the SDK call site.
    assert fake_client.submit_order.call_count == 1
    req = fake_client.submit_order.call_args.args[0]
    # Stop trigger and take-profit limit must be penny-aligned.
    assert req.stop_loss.stop_price == 635.11
    assert req.take_profit.limit_price == 744.35


def test_submit_order_defaults_to_day_tif() -> None:
    """Default time_in_force maps to DAY (bracket expires at the close)."""
    broker = AlpacaPaperBroker(api_key="dummy", api_secret="dummy")
    fake_client = _fake_trading_client_response("CRWD")
    broker._trading = fake_client  # type: ignore[assignment]

    broker.submit_order(_make_bracket_order(stop=Decimal("90"), take=Decimal("110")))
    req = fake_client.submit_order.call_args.args[0]
    assert str(req.time_in_force).endswith("DAY")


def test_submit_order_gtc_tif_for_overnight_bracket() -> None:
    """time_in_force='gtc' maps to GTC so the bracket survives overnight.

    NOTE: whether Alpaca accepts GTC on a market-parent bracket must be
    confirmed on live paper before enabling protect_overnight (plan Phase 2).
    """
    broker = AlpacaPaperBroker(api_key="dummy", api_secret="dummy")
    fake_client = _fake_trading_client_response("CRWD")
    broker._trading = fake_client  # type: ignore[assignment]

    order = ProposedOrder(
        symbol="CRWD",
        side=OrderSide.BUY,
        qty=Decimal("2"),
        order_type=OrderType.MARKET,
        stop_price=Decimal("90"),
        take_price=Decimal("110"),
        client_order_id="test-CRWD",
        time_in_force="gtc",
    )
    broker.submit_order(order)
    req = fake_client.submit_order.call_args.args[0]
    assert str(req.time_in_force).endswith("GTC")


def test_submit_order_quantizes_oto_stop_only() -> None:
    broker = AlpacaPaperBroker(api_key="dummy", api_secret="dummy")
    fake_client = _fake_trading_client_response("AAPL")
    broker._trading = fake_client  # type: ignore[assignment]

    order = ProposedOrder(
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=Decimal("10"),
        order_type=OrderType.MARKET,
        stop_price=Decimal("192.4444444444"),
        take_price=None,
        client_order_id="test-AAPL",
    )
    broker.submit_order(order)
    req = fake_client.submit_order.call_args.args[0]
    assert req.stop_loss.stop_price == 192.44
    # take_profit must not be attached when only a stop is supplied.
    assert not hasattr(req, "take_profit") or req.take_profit is None


def test_submit_order_quantizes_limit_price() -> None:
    broker = AlpacaPaperBroker(api_key="dummy", api_secret="dummy")
    fake_client = _fake_trading_client_response("SPY")
    broker._trading = fake_client  # type: ignore[assignment]

    order = ProposedOrder(
        symbol="SPY",
        side=OrderSide.SELL,
        qty=Decimal("5"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("541.6789999999"),
        client_order_id="test-SPY",
    )
    broker.submit_order(order)
    req = fake_client.submit_order.call_args.args[0]
    assert req.limit_price == 541.68


# --- replace_stop_price -----------------------------------------------------


def _make_stop_leg(
    *,
    order_id: str = "leg-1",
    order_type: str = "stop",
    stop_price: str | float | None = "95.00",
) -> MagicMock:
    """Build a mock open order shaped like an Alpaca stop-leg child.

    The adapter reads `order.order_type` (string), `order.id` (uuid-ish), and
    `order.stop_price` (str/float). All three must be present for the
    `replace_stop_price` flow to do the right thing.
    """
    leg = MagicMock()
    leg.id = order_id
    leg.order_type = order_type
    leg.stop_price = stop_price
    return leg


def _broker_with_open_orders(open_orders: list[MagicMock]) -> tuple[
    AlpacaPaperBroker, MagicMock
]:
    broker = AlpacaPaperBroker(api_key="dummy", api_secret="dummy")
    client = MagicMock()
    client.get_orders.return_value = open_orders
    broker._trading = client  # type: ignore[assignment]
    return broker, client


def test_replace_stop_price_success_calls_replace_with_quantized_price():
    """The happy path: one open stop leg, new price differs, the adapter
    calls `replace_order_by_id` with the quantized stop_price."""
    leg = _make_stop_leg(stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])

    result = broker.replace_stop_price("SPY", Decimal("96.50"))

    assert result is True
    assert client.replace_order_by_id.call_count == 1
    call = client.replace_order_by_id.call_args
    assert call.kwargs["order_id"] == "leg-1"
    # The ReplaceOrderRequest carries the on-tick value; float comparison is
    # fine here because 96.50 is exactly representable.
    assert call.kwargs["order_data"].stop_price == 96.50


def test_replace_stop_price_idempotent_no_op_when_existing_matches():
    """If the broker-side stop already equals the (quantized) target, the
    adapter MUST NOT issue a redundant replace call. Returns True to signal
    the leg exists and is at the requested level."""
    leg = _make_stop_leg(stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])

    result = broker.replace_stop_price("SPY", Decimal("95.00"))

    assert result is True
    assert client.replace_order_by_id.call_count == 0


def test_replace_stop_price_idempotent_after_quantization_of_sub_penny_input():
    """Sub-penny dust input that quantizes to the existing stop should also
    be treated as a no-op. Otherwise the trailing engine would chase its
    own quantization round-off every poll."""
    leg = _make_stop_leg(stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])

    # 94.9999999 → 95.00 via _quantize_to_tick → matches existing.
    result = broker.replace_stop_price("SPY", Decimal("94.9999999"))

    assert result is True
    assert client.replace_order_by_id.call_count == 0


def test_replace_stop_price_returns_false_when_no_stop_leg_found():
    """No open orders at all (or only non-stop orders) → returns False so
    the session loop knows the position is already unprotected by a bracket
    child and can skip the trail update without raising."""
    broker, client = _broker_with_open_orders([])

    result = broker.replace_stop_price("SPY", Decimal("96.50"))

    assert result is False
    assert client.replace_order_by_id.call_count == 0


def test_replace_stop_price_multi_live_leg_refuses_and_warns(caplog):
    """Two genuinely live stop legs on one symbol means more than one open
    bracket — the adapter can't know which one the caller's new_stop targets.
    It must refuse (return False, issue NO replace) and warn, leaving the
    existing stops in place rather than ratcheting the wrong one."""
    leg_a = _make_stop_leg(order_id="leg-a", stop_price="95.00")
    leg_b = _make_stop_leg(order_id="leg-b", stop_price="94.50")
    broker, client = _broker_with_open_orders([leg_a, leg_b])

    with caplog.at_level(logging.WARNING, logger="trading_bot.execution.alpaca_paper"):
        result = broker.replace_stop_price("SPY", Decimal("96.50"))

    assert result is False
    assert client.replace_order_by_id.call_count == 0
    warn_records = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("multiple live stop legs" in r.message for r in warn_records)


def _make_parent_with_legs(legs: list[MagicMock], *, order_type: str = "market") -> MagicMock:
    """A bracket parent order with nested child `.legs`.

    Mirrors what Alpaca returns for `get_orders(..., nested=True)` while the
    parent market order is still working: the stop/take children are `held`
    and rolled under the parent rather than surfaced as top-level orders.
    """
    parent = MagicMock()
    parent.id = "parent-1"
    parent.order_type = order_type
    parent.stop_price = None
    parent.legs = legs
    return parent


def test_replace_stop_price_finds_nested_held_stop_leg():
    """Regression for the 2026-05-27 'no open stop leg found' spam: while the
    bracket parent is still working, its stop child is nested under the
    parent's `.legs`. The adapter must traverse into `.legs` to find it."""
    stop_child = _make_stop_leg(order_id="stop-child", stop_price="95.00")
    parent = _make_parent_with_legs([stop_child])
    broker, client = _broker_with_open_orders([parent])

    result = broker.replace_stop_price("SPY", Decimal("96.50"))

    assert result is True
    assert client.replace_order_by_id.call_count == 1
    assert client.replace_order_by_id.call_args.kwargs["order_id"] == "stop-child"
    # The query must request nested orders or the child is invisible.
    assert client.get_orders.call_args.kwargs["filter"].nested is True


def test_replace_stop_price_nested_take_only_returns_false():
    """A parent whose only nested child is the take-profit (limit) leg has no
    stop to replace — must return False, not mistake the limit leg for a stop."""
    take_child = _make_stop_leg(order_id="take-child", order_type="limit")
    parent = _make_parent_with_legs([take_child])
    broker, client = _broker_with_open_orders([parent])

    result = broker.replace_stop_price("SPY", Decimal("96.50"))

    assert result is False
    assert client.replace_order_by_id.call_count == 0


def _make_leg(*, order_id, order_type, status, stop_price=None, limit_price=None):
    """A leg/order shaped like the *live* SDK return: `order_type` and `status`
    are enum reprs ("OrderType.STOP" / "OrderStatus.HELD"), not bare lowercase
    strings. The lowercase-string mocks above never exercised the enum-repr
    path, which is why both the OPEN-filter and exact-match-type bugs survived.
    """
    o = MagicMock()
    o.id = order_id
    o.order_type = order_type
    o.status = status
    o.stop_price = stop_price
    o.limit_price = limit_price
    o.legs = None
    return o


def test_replace_stop_price_finds_held_stop_under_filled_parent():
    """Regression for the 2026-06-02 XLE failure: once the bracket's market
    parent FILLS, the stop child sits HELD nested under the (now closed) parent.
    A status=OPEN query drops both, surfacing only the live take-profit limit
    leg. The adapter must query ALL and find the held stop child."""
    from alpaca.trading.enums import QueryOrderStatus

    held_stop = _make_leg(
        order_id="stop-child", order_type="OrderType.STOP",
        status="OrderStatus.HELD", stop_price="56.41",
    )
    live_limit = _make_leg(
        order_id="take-child", order_type="OrderType.LIMIT",
        status="OrderStatus.NEW", limit_price="60.59",
    )
    filled_parent = MagicMock()
    filled_parent.id = "parent-filled"
    filled_parent.order_type = "OrderType.MARKET"
    filled_parent.status = "OrderStatus.FILLED"
    filled_parent.stop_price = None
    filled_parent.legs = [live_limit, held_stop]

    broker, client = _broker_with_open_orders([filled_parent])

    result = broker.replace_stop_price("XLE", Decimal("56.49"))

    assert result is True
    assert client.replace_order_by_id.call_count == 1
    assert client.replace_order_by_id.call_args.kwargs["order_id"] == "stop-child"
    # Must query ALL — OPEN would never return the closed parent or its held leg.
    assert client.get_orders.call_args.kwargs["filter"].status == QueryOrderStatus.ALL
    assert client.get_orders.call_args.kwargs["filter"].nested is True
    # And it must be bounded so the live bracket can't silently fall off the
    # default page and revert to "no stop found" (frozen trail).
    assert client.get_orders.call_args.kwargs["filter"].limit is not None


def test_replace_stop_price_ignores_stopped_stop_leg():
    """A stop leg in `STOPPED` (already triggered/executed) is terminal — the
    adapter must not attempt to re-replace a fired stop."""
    fired = _make_leg(
        order_id="fired-stop", order_type="OrderType.STOP",
        status="OrderStatus.STOPPED", stop_price="56.41",
    )
    broker, client = _broker_with_open_orders([fired])

    result = broker.replace_stop_price("XLE", Decimal("56.49"))

    assert result is False
    assert client.replace_order_by_id.call_count == 0


def test_replace_stop_price_ignores_terminal_canceled_stop_leg():
    """status=ALL also returns prior completed brackets. A CANCELED stop child
    is dead — it must not be selected or replaced."""
    dead_stop = _make_leg(
        order_id="dead-stop", order_type="OrderType.STOP",
        status="OrderStatus.CANCELED", stop_price="59.33",
    )
    done_parent = MagicMock()
    done_parent.id = "parent-done"
    done_parent.order_type = "OrderType.MARKET"
    done_parent.status = "OrderStatus.FILLED"
    done_parent.stop_price = None
    done_parent.legs = [dead_stop]

    broker, client = _broker_with_open_orders([done_parent])

    result = broker.replace_stop_price("XLE", Decimal("56.49"))

    assert result is False
    assert client.replace_order_by_id.call_count == 0


def test_replace_stop_price_picks_live_stop_over_dead_bracket(caplog):
    """With a prior completed bracket (CANCELED stop) AND the current live
    bracket (HELD stop) both returned by status=ALL, the adapter must replace
    the live HELD leg and must NOT warn about multiple stop legs."""
    dead_stop = _make_leg(
        order_id="dead-stop", order_type="OrderType.STOP",
        status="OrderStatus.CANCELED", stop_price="59.33",
    )
    held_stop = _make_leg(
        order_id="live-stop", order_type="OrderType.STOP",
        status="OrderStatus.HELD", stop_price="56.41",
    )
    done_parent = MagicMock()
    done_parent.id = "p-done"; done_parent.order_type = "OrderType.MARKET"
    done_parent.status = "OrderStatus.FILLED"; done_parent.stop_price = None
    done_parent.legs = [dead_stop]
    live_parent = MagicMock()
    live_parent.id = "p-live"; live_parent.order_type = "OrderType.MARKET"
    live_parent.status = "OrderStatus.FILLED"; live_parent.stop_price = None
    live_parent.legs = [held_stop]

    broker, client = _broker_with_open_orders([live_parent, done_parent])

    with caplog.at_level(logging.WARNING, logger="trading_bot.execution.alpaca_paper"):
        result = broker.replace_stop_price("XLE", Decimal("56.49"))

    assert result is True
    assert client.replace_order_by_id.call_count == 1
    assert client.replace_order_by_id.call_args.kwargs["order_id"] == "live-stop"
    assert not any("multiple stop legs" in r.message for r in caplog.records)


def test_replace_stop_price_ignores_broker_trailing_stop_leg():
    """A broker-side `trailing_stop` family leg (enum repr "OrderType.TRAILING_STOP")
    must NOT be picked up — the bot trails stops itself; overwriting a
    broker-managed trailing leg would be wrong."""
    trail = _make_leg(
        order_id="trail", order_type="OrderType.TRAILING_STOP",
        status="OrderStatus.NEW", stop_price="55.00",
    )
    broker, client = _broker_with_open_orders([trail])

    result = broker.replace_stop_price("XLE", Decimal("56.49"))

    assert result is False
    assert client.replace_order_by_id.call_count == 0


@pytest.mark.parametrize("order_type", ["stop", "STOP", "stop_loss", "stop_limit"])
def test_replace_stop_price_detects_stop_family(order_type):
    """Alpaca surfaces the stop child as one of several order_type strings
    depending on bracket flavor and SDK version. Match the family."""
    leg = _make_stop_leg(order_type=order_type, stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])

    result = broker.replace_stop_price("SPY", Decimal("96.50"))

    assert result is True
    assert client.replace_order_by_id.call_count == 1


@pytest.mark.parametrize("order_type", ["limit", "market", "trailing_stop"])
def test_replace_stop_price_ignores_non_stop_family(order_type):
    """Limit/market/trailing_stop are NOT stop legs. The adapter must skip
    them and return False — touching a take-profit or broker-side trailing
    leg as if it were the stop would be a serious correctness bug.

    `trailing_stop` is intentionally excluded from the stop-leg family: it's
    a distinct Alpaca order class with broker-managed dynamic offsets, and
    the bot's trailing logic is intentionally bot-side. The production code
    now uses an explicit family set, so this case is asserted (not skipped).
    """
    leg = _make_stop_leg(order_type=order_type, stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])

    result = broker.replace_stop_price("SPY", Decimal("96.50"))

    assert result is False
    assert client.replace_order_by_id.call_count == 0


def test_replace_stop_price_quantizes_sub_penny_input():
    """The dust-from-strategy-math case that motivates _quantize_to_tick in
    submit_order applies equally to the replace path. A sub-penny input must
    be rounded before reaching the SDK."""
    leg = _make_stop_leg(stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])

    # Decimal("100.123") → quantize to 100.12 at penny tick.
    result = broker.replace_stop_price("SPY", Decimal("100.123"))

    assert result is True
    call = client.replace_order_by_id.call_args
    assert call.kwargs["order_data"].stop_price == 100.12


def _make_status_error(status_code: int, message: str) -> Exception:
    """Construct an exception that `_classify_broker_error` will route on
    `status_code` — the only attribute the classifier reads."""
    err = Exception(message)
    err.status_code = status_code  # type: ignore[attr-defined]
    return err


def test_replace_stop_price_auth_error_on_401():
    """A 401 from the replace endpoint must raise BrokerAuthError so the
    session loop halts instead of burning the consecutive_failures budget."""
    leg = _make_stop_leg(stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])
    client.replace_order_by_id.side_effect = _make_status_error(
        401, "unauthorized"
    )

    with pytest.raises(BrokerAuthError):
        broker.replace_stop_price("SPY", Decimal("96.50"))


def test_replace_stop_price_validation_error_on_422():
    """A 422 must raise BrokerValidationError — bot-side payload bug, retrying
    is useless."""
    leg = _make_stop_leg(stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])
    client.replace_order_by_id.side_effect = _make_status_error(
        422, "stop_price below last trade"
    )

    with pytest.raises(BrokerValidationError):
        broker.replace_stop_price("SPY", Decimal("96.50"))


def test_replace_stop_price_generic_broker_error_on_unclassified():
    """Anything else (no status_code, 403, 500) must raise generic BrokerError
    — caller decides whether to retry."""
    leg = _make_stop_leg(stop_price="95.00")
    broker, client = _broker_with_open_orders([leg])
    client.replace_order_by_id.side_effect = RuntimeError("connection reset")

    with pytest.raises(BrokerError) as excinfo:
        broker.replace_stop_price("SPY", Decimal("96.50"))
    # Confirm it's the *base* BrokerError, not a halt-class subtype.
    assert not isinstance(excinfo.value, (BrokerAuthError, BrokerValidationError))


def test_replace_stop_price_propagates_get_orders_failure():
    """The pre-flight `get_orders` call can also fail. The same error taxonomy
    applies: a 401 there must surface as BrokerAuthError too."""
    broker = AlpacaPaperBroker(api_key="dummy", api_secret="dummy")
    client = MagicMock()
    client.get_orders.side_effect = _make_status_error(401, "unauthorized")
    broker._trading = client  # type: ignore[assignment]

    with pytest.raises(BrokerAuthError):
        broker.replace_stop_price("SPY", Decimal("96.50"))
