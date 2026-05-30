"""Session-level integration tests for the trailing-stop ratchet.

The pure ratchet math is pinned in `tests/test_risk/test_trailing.py`.
The broker-adapter `replace_stop_price` is pinned in
`tests/test_execution/test_alpaca_paper.py`. This module tests the
glue in `trading_bot.execution.session`:

  • `_record_entry` arms trail state correctly based on the per-strategy
    policy and the presence of an initial stop on the order.
  • `_check_stops_and_takes` walks the ratchet, calls the broker, updates
    the in-memory entry record, and emits the TRAIL audit-log line.
  • Errors from `replace_stop_price` are absorbed (BrokerError) or
    propagated (BrokerAuthError, BrokerValidationError) per the risk
    taxonomy.
  • Per-strategy gating: `insider` is OFF by default in the live policy
    and must NOT arm even if a stop was provided.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.contracts import OrderSide, OrderType, ProposedOrder
from trading_bot.execution.broker import (
    BrokerAuthError,
    BrokerError,
    BrokerOrderResponse,
    BrokerPosition,
    BrokerValidationError,
)
from trading_bot.execution.session import (
    SessionState,
    _check_stops_and_takes,
    _record_entry,
)

from tests.test_execution.fakes import FakeBroker


_ENTRY_TIME = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)


# --- helpers ----------------------------------------------------------------


def _make_order(
    *,
    symbol: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    qty: Decimal = Decimal("10"),
    stop_price: Decimal | None = Decimal("95"),
    take_price: Decimal | None = Decimal("110"),
) -> ProposedOrder:
    return ProposedOrder(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        stop_price=stop_price,
        take_price=take_price,
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


def _session_with_policy(policy: dict[str, bool]) -> SessionState:
    sess = SessionState()
    sess.trailing_stop_policy = policy
    return sess


class TrailingFakeBroker(FakeBroker):
    """FakeBroker plus a `replace_stop_price` shim that records calls.

    Behavior knobs:
      • `replace_stop_calls` — list of (symbol, new_stop) tuples for assertions.
      • `replace_stop_raise` — exception instance to raise instead of returning.
      • `replace_stop_return` — value to return on a successful call (default True).
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.replace_stop_calls: list[tuple[str, Decimal]] = []
        self.replace_stop_raise: Exception | None = None
        self.replace_stop_return: bool = True

    def replace_stop_price(self, symbol: str, new_stop_price: Decimal) -> bool:
        self.replace_stop_calls.append((symbol, new_stop_price))
        if self.replace_stop_raise is not None:
            raise self.replace_stop_raise
        return self.replace_stop_return


def _seed_position(
    broker: FakeBroker,
    symbol: str,
    qty: Decimal,
    price: Decimal,
) -> None:
    broker.positions[symbol] = BrokerPosition(
        symbol=symbol,
        qty=qty,
        avg_entry_price=price,
        market_value=qty * price,
    )


# --- _record_entry: trail-arming gate --------------------------------------


def test_record_entry_arms_trail_when_policy_enabled_and_stop_present():
    """ORB-style entry with the policy enabling 'orb' and a stop on the
    order: trail state must be initialized with the dollar-offset baseline
    and `trail_extreme` seeded to entry_price."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"))
    resp = _make_response(fill_price=Decimal("100"))

    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    entry = sess.open_entries["SPY"]
    assert entry["trail_enabled"] is True
    assert entry["trail_offset"] == Decimal("5")  # 100 - 95
    assert entry["trail_extreme"] == Decimal("100")
    assert entry["stop_price"] == Decimal("95")


def test_record_entry_does_not_arm_when_strategy_missing_from_policy():
    """If the strategy name has no entry in the policy dict, default is False
    even with a stop on the order. Catches the 'insider got accidentally
    enabled because someone updated the orb config' regression."""
    sess = _session_with_policy({"orb": True})  # no 'insider' key
    order = _make_order(stop_price=Decimal("95"))
    resp = _make_response(fill_price=Decimal("100"))

    _record_entry(sess, "SPY", "long", order, resp, strategy="insider")

    assert sess.open_entries["SPY"]["trail_enabled"] is False


def test_record_entry_does_not_arm_when_strategy_explicitly_disabled():
    """The live insider config sets enable_trailing_stop=False; verify the
    explicit False value gates the arming the same way a missing key does."""
    sess = _session_with_policy(
        {"orb": True, "pullback": True, "insider": False}
    )
    order = _make_order(stop_price=Decimal("95"))
    resp = _make_response(fill_price=Decimal("100"))

    _record_entry(sess, "SPY", "long", order, resp, strategy="insider")

    assert sess.open_entries["SPY"]["trail_enabled"] is False


def test_record_entry_does_not_arm_when_stop_price_is_none():
    """A policy-enabled strategy with no initial stop has no offset to
    compute. The trail engine refuses to fabricate one — `trail_enabled`
    must be False even though the policy says yes."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=None)
    resp = _make_response(fill_price=Decimal("100"))

    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    assert sess.open_entries["SPY"]["trail_enabled"] is False


def test_record_entry_arms_trail_for_short_with_correct_offset():
    """Short-side trail offset is `initial_stop - entry_price` (positive).
    Entry at 30, stop above at 31 → offset 1.0."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(
        symbol="SMCI",
        side=OrderSide.SELL,
        stop_price=Decimal("31"),
        take_price=Decimal("28"),
    )
    resp = _make_response(
        symbol="SMCI",
        side=OrderSide.SELL,
        fill_price=Decimal("30"),
    )

    _record_entry(sess, "SMCI", "short", order, resp, strategy="orb")

    entry = sess.open_entries["SMCI"]
    assert entry["trail_enabled"] is True
    assert entry["trail_offset"] == Decimal("1")
    assert entry["trail_extreme"] == Decimal("30")


# --- _check_stops_and_takes: ratchet behavior -----------------------------


def test_ratchet_fires_when_price_advances_long():
    """Long entry at 100, stop 95, trail offset 5. Latest price 108 → new
    stop 103. `replace_stop_price` is called once, `entry["stop_price"]`
    is updated to the new value, and a 'trailing stop ratcheted' info log
    is emitted."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("108")})
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    with caplog_at_session_info() as records:
        _check_stops_and_takes(
            broker=broker, sess=sess, sleep_fn=lambda _s: None
        )

    assert broker.replace_stop_calls == [("SPY", Decimal("103"))]
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("103")
    assert sess.open_entries["SPY"]["trail_extreme"] == Decimal("108")
    # Position stays open — price 108 is below the new stop 103 and below
    # the take 130.
    assert "SPY" in sess.open_entries
    assert any(r.message == "trailing stop ratcheted" for r in records)


def test_ratchet_fires_when_price_drops_short():
    """Mirror for a short: entry 30, stop 31, offset 1. Latest price 25 →
    new stop 26. Pinned the same way as the long case."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(
        symbol="SMCI",
        side=OrderSide.SELL,
        stop_price=Decimal("31"),
        take_price=Decimal("20"),
    )
    resp = _make_response(
        symbol="SMCI",
        side=OrderSide.SELL,
        fill_price=Decimal("30"),
    )
    _record_entry(sess, "SMCI", "short", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SMCI": Decimal("25")})
    _seed_position(broker, "SMCI", Decimal("-10"), Decimal("30"))

    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)

    assert broker.replace_stop_calls == [("SMCI", Decimal("26"))]
    assert sess.open_entries["SMCI"]["stop_price"] == Decimal("26")
    assert sess.open_entries["SMCI"]["trail_extreme"] == Decimal("25")


def test_ratchet_no_op_on_first_poll_does_not_call_broker():
    """First poll: extreme equals entry price, so the candidate stop equals
    the existing stop. The session loop MUST NOT call `replace_stop_price`
    — that's the load-bearing safety invariant. Same content as the pure
    test, just verified at the integration layer."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("100")})
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)

    assert broker.replace_stop_calls == []
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("95")


def test_ratchet_does_not_run_when_trail_disabled():
    """Strategy not in the policy → no trail state armed → no replace call
    even if the price moves substantially in favor."""
    sess = _session_with_policy({"orb": True, "pullback": True, "insider": False})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="insider")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("125")})
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)

    assert broker.replace_stop_calls == []
    # The original stop is preserved untouched.
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("95")
    # `trail_enabled` is still False even after the poll.
    assert sess.open_entries["SPY"]["trail_enabled"] is False


def test_ratchet_absorbs_generic_broker_error_and_keeps_prior_stop():
    """A generic BrokerError on `replace_stop_price` MUST NOT propagate —
    the loop logs and continues with the prior stop. Otherwise a single
    flaky replace call would kill the session and orphan all positions
    until the operator reconciles."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("108")})
    broker.replace_stop_raise = BrokerError("broker connection reset")
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    # Must not raise.
    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)

    # Replace attempted exactly once.
    assert broker.replace_stop_calls == [("SPY", Decimal("103"))]
    # Prior stop preserved.
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("95")
    # Position still open.
    assert "SPY" in sess.open_entries


def test_ratchet_propagates_broker_auth_error():
    """A 401 from the broker is halt-class — it MUST surface so the session
    loop can take down the run cleanly via the post-loop flatten path."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("108")})
    broker.replace_stop_raise = BrokerAuthError("revoked key")
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    with pytest.raises(BrokerAuthError):
        _check_stops_and_takes(
            broker=broker, sess=sess, sleep_fn=lambda _s: None
        )


def test_ratchet_propagates_broker_validation_error():
    """422 = malformed payload, also halt-class."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("108")})
    broker.replace_stop_raise = BrokerValidationError("stop below last trade")
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    with pytest.raises(BrokerValidationError):
        _check_stops_and_takes(
            broker=broker, sess=sess, sleep_fn=lambda _s: None
        )


def test_ratchet_records_false_replacement_keeps_prior_stop():
    """`replace_stop_price` returning False (no broker-side leg found) means
    the broker bracket already fired or was canceled — we should NOT update
    the in-memory `stop_price` to the new computed value, because there is
    nothing on the broker side at that level anymore."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("108")})
    broker.replace_stop_return = False  # No leg found
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)

    assert broker.replace_stop_calls == [("SPY", Decimal("103"))]
    # In-memory stop preserved (loop sets stop_price only on True return).
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("95")


def test_ratchet_progressive_tightening_over_multiple_polls():
    """Walk price up over three polls: 105 → 110 → 108 (pullback). The
    ratchet should advance the stop on polls 1 and 2 but NOT roll back on
    poll 3 even though the price drops — the trail is monotonic. This is
    the integration check that pairs with the pure monotonicity tests."""
    sess = _session_with_policy({"orb": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="orb")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("105")})
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    # Poll 1: price 105 → new stop 100.
    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("100")
    assert sess.open_entries["SPY"]["trail_extreme"] == Decimal("105")

    # Poll 2: price 110 → new stop 105.
    broker.latest_prices["SPY"] = Decimal("110")
    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("105")
    assert sess.open_entries["SPY"]["trail_extreme"] == Decimal("110")

    # Poll 3: price falls to 108 — extreme stays at 110, stop unchanged.
    broker.latest_prices["SPY"] = Decimal("108")
    _check_stops_and_takes(broker=broker, sess=sess, sleep_fn=lambda _s: None)
    assert sess.open_entries["SPY"]["stop_price"] == Decimal("105")
    assert sess.open_entries["SPY"]["trail_extreme"] == Decimal("110")

    # Two ratchet calls total — poll 3 was a no-op on the broker.
    assert len(broker.replace_stop_calls) == 2
    assert broker.replace_stop_calls[0] == ("SPY", Decimal("100"))
    assert broker.replace_stop_calls[1] == ("SPY", Decimal("105"))


def test_trail_event_logged_with_strategy_attribution(caplog):
    """The TRAIL audit log line must carry the strategy label so a downstream
    P&L attribution view of the audit log can split per bot."""
    sess = _session_with_policy({"pullback": True})
    order = _make_order(stop_price=Decimal("95"), take_price=Decimal("130"))
    resp = _make_response(fill_price=Decimal("100"))
    _record_entry(sess, "SPY", "long", order, resp, strategy="pullback")

    broker = TrailingFakeBroker(latest_prices={"SPY": Decimal("108")})
    _seed_position(broker, "SPY", Decimal("10"), Decimal("100"))

    with caplog.at_level(logging.INFO, logger="trading_bot.execution.session"):
        _check_stops_and_takes(
            broker=broker, sess=sess, sleep_fn=lambda _s: None
        )

    trail_records = [
        r for r in caplog.records if r.message == "trailing stop ratcheted"
    ]
    assert len(trail_records) == 1
    assert trail_records[0].strategy == "pullback"
    assert trail_records[0].old_stop_price == "95"
    assert trail_records[0].new_stop_price == "103"


# --- caplog helper ---------------------------------------------------------


from contextlib import contextmanager
import logging as _logging


@contextmanager
def caplog_at_session_info():
    """Capture INFO+ records from the session logger into a list.

    Standalone helper so `test_ratchet_fires_when_price_advances_long` can
    assert on the TRAIL log record without depending on the order pytest's
    `caplog` fixture interleaves with the FakeBroker's own logging.
    """
    logger = _logging.getLogger("trading_bot.execution.session")
    records: list[_logging.LogRecord] = []

    class _Handler(_logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler(level=_logging.INFO)
    prior_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(_logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
