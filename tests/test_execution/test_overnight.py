"""Tests for the overnight-holding feature (plan Phases 2 & 3).

Phase 2: when SessionConfig.protect_overnight is set, entries are submitted GTC
so the broker-side bracket survives the close. Phase 3: a clean exit that leaves
positions open writes a CARRIED_*.json record, and a matching, fresh record is
ADOPTED on the next start instead of hitting the orphan refusal.

The whole feature is gated behind protect_overnight (default off); the default
DAY-bracket / refuse-orphans behavior is covered in test_session.py.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_bot.contracts import OrderSide, RiskParams
from trading_bot.execution import SessionConfig, run_session
from trading_bot.execution.broker import BrokerPosition
from trading_bot.execution.session import _adopt_carried_positions
from trading_bot.strategy import ORBStrategy, load_config

from tests.test_execution.fakes import FakeBroker
from tests.test_strategy.fixtures import make_synthetic_session

_SESSION_DATE = date(2026, 1, 5)
_AFTER_ENTRY = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)  # before the 15:55 flat


def _strategy():
    return ORBStrategy(load_config())


def _risk():
    return RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
        max_position_count=5,
        symbol_whitelist=frozenset({"SPY"}),
    )


def _config(**kw):
    base = dict(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        max_signal_age_minutes=None,
        force_ignore_unreconciled=False,
    )
    base.update(kw)
    return SessionConfig(**base)


def _run(broker, **cfg):
    return run_session(
        broker,
        _strategy(),
        _config(**cfg),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )


def _write_carried(carried_dir, positions, *, et_date="2026-01-05"):
    """Write a CARRIED record directly so the adoption path can be exercised."""
    carried_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": "20260104T210000Z",
        "created_at_utc": "2026-01-04T21:00:00+00:00",
        "et_date": et_date,
        "positions": positions,
    }
    (carried_dir / "CARRIED_20260104T210000Z.json").write_text(json.dumps(payload))


def _carried_pos(symbol="SPY", qty="10", price="100", strategy="orb"):
    return {
        "symbol": symbol,
        "side": "long",
        "qty": qty,
        "entry_price": price,
        "entry_time": "2026-01-04T15:00:00+00:00",
        "stop_price": "90",
        "take_price": "120",
        "strategy": strategy,
        "trail_enabled": False,
        "trail_offset": "0",
        "trail_extreme": price,
    }


# --- Phase 2: GTC entries when protect_overnight is on ----------------------


def test_protect_overnight_submits_gtc_entry():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(bars_by_symbol={"SPY": bars}, latest_prices={"SPY": Decimal("100")})
    _run(broker, protect_overnight=True, force_ignore_unreconciled=True)
    buys = [o for o in broker.submitted_orders if o.side == OrderSide.BUY]
    assert len(buys) == 1
    assert buys[0].time_in_force == "gtc"


def test_default_submits_day_entry():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(bars_by_symbol={"SPY": bars}, latest_prices={"SPY": Decimal("100")})
    _run(broker, force_ignore_unreconciled=True)  # protect_overnight defaults False
    buys = [o for o in broker.submitted_orders if o.side == OrderSide.BUY]
    assert len(buys) == 1
    assert buys[0].time_in_force == "day"


# --- Phase 3: carried-position record on exit -------------------------------


def test_carried_record_written_on_clean_exit_with_open_position(_isolated_ops_dirs):
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(bars_by_symbol={"SPY": bars}, latest_prices={"SPY": Decimal("100")})
    # Entry opens at ~15:30; the 15:55 flat does not fire in one iteration, so
    # the position is left open at exit and must be recorded.
    _run(broker, protect_overnight=True, force_ignore_unreconciled=True)
    assert "SPY" in broker.get_positions()
    records = list((_isolated_ops_dirs["carried"]).glob("CARRIED_*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text())
    assert [p["symbol"] for p in payload["positions"]] == ["SPY"]


def test_no_carried_record_when_protect_overnight_off(_isolated_ops_dirs):
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(bars_by_symbol={"SPY": bars}, latest_prices={"SPY": Decimal("100")})
    _run(broker, force_ignore_unreconciled=True)  # feature off
    assert not list((_isolated_ops_dirs["carried"]).glob("CARRIED_*.json"))


# --- Phase 3: adoption on restart -------------------------------------------


def _flat_broker():
    """A broker that produces no new entries (no breakout) but holds SPY."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="none")
    return FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        positions={"SPY": BrokerPosition("SPY", Decimal("10"), Decimal("100"), Decimal("1000"))},
    )


def test_matching_carried_record_is_adopted(_isolated_ops_dirs):
    _write_carried(_isolated_ops_dirs["carried"], [_carried_pos()])
    broker = _flat_broker()
    state = _run(broker, protect_overnight=True)  # force_ignore stays False
    # Adopted into open_entries so stop/take management continues.
    assert "SPY" in state.open_entries
    assert state.open_entries["SPY"]["qty"] == Decimal("10")
    # The consumed record is cleared; since the position is STILL open at exit,
    # a fresh record (this run's id) is written for the next start.
    records = list((_isolated_ops_dirs["carried"]).glob("CARRIED_*.json"))
    assert len(records) == 1
    assert "20260104" not in records[0].name  # the adopted (old) record is gone


def test_orphan_not_in_record_refuses(_isolated_ops_dirs):
    # Record covers SPY, but the broker also holds an unrecorded QQQ → orphan.
    _write_carried(_isolated_ops_dirs["carried"], [_carried_pos()])
    broker = _flat_broker()
    broker.positions["QQQ"] = BrokerPosition("QQQ", Decimal("5"), Decimal("50"), Decimal("250"))
    with pytest.raises(RuntimeError, match="Refusing to start"):
        _run(broker, protect_overnight=True)


def test_broker_holds_more_than_recorded_refuses(_isolated_ops_dirs):
    _write_carried(_isolated_ops_dirs["carried"], [_carried_pos(qty="10")])
    broker = _flat_broker()
    broker.positions["SPY"] = BrokerPosition("SPY", Decimal("15"), Decimal("100"), Decimal("1500"))
    with pytest.raises(RuntimeError, match="Refusing to start"):
        _run(broker, protect_overnight=True)


def test_stale_carried_record_refuses(_isolated_ops_dirs):
    # et_date far in the past → stale → ignored → orphan refusal.
    _write_carried(_isolated_ops_dirs["carried"], [_carried_pos()], et_date="2025-12-01")
    broker = _flat_broker()
    with pytest.raises(RuntimeError, match="Refusing to start"):
        _run(broker, protect_overnight=True)


def test_protect_overnight_off_still_refuses_with_record(_isolated_ops_dirs):
    # A carried record present but the feature gated off → orphan refusal stands.
    _write_carried(_isolated_ops_dirs["carried"], [_carried_pos()])
    broker = _flat_broker()
    with pytest.raises(RuntimeError, match="Refusing to start"):
        _run(broker)  # protect_overnight=False


# --- Phase 3: _adopt_carried_positions matching matrix (pure) ---------------


def _rec(**kw):
    base = dict(symbol="SPY", side="long", qty="10", entry_price="100",
                entry_time="2026-01-04T15:00:00+00:00", stop_price="90",
                take_price="120", strategy="orb", trail_enabled=False,
                trail_offset="0", trail_extreme="100")
    base.update(kw)
    return {"positions": [base]}


def test_adopt_exact_match():
    broker = {"SPY": BrokerPosition("SPY", Decimal("10"), Decimal("100"), Decimal("1000"))}
    out = _adopt_carried_positions(_rec(), broker)
    assert out is not None and out["SPY"]["qty"] == Decimal("10")


def test_adopt_broker_qty_is_authority_when_less():
    # Partial overnight exit: broker holds less than recorded → adopt broker qty.
    broker = {"SPY": BrokerPosition("SPY", Decimal("7"), Decimal("100"), Decimal("700"))}
    out = _adopt_carried_positions(_rec(qty="10"), broker)
    assert out is not None and out["SPY"]["qty"] == Decimal("7")


def test_adopt_refuses_more_than_recorded():
    broker = {"SPY": BrokerPosition("SPY", Decimal("12"), Decimal("100"), Decimal("1200"))}
    assert _adopt_carried_positions(_rec(qty="10"), broker) is None


def test_adopt_refuses_side_mismatch():
    # Record says long; broker shows short (negative qty).
    broker = {"SPY": BrokerPosition("SPY", Decimal("-10"), Decimal("100"), Decimal("-1000"))}
    assert _adopt_carried_positions(_rec(side="long"), broker) is None


def test_adopt_refuses_price_drift():
    broker = {"SPY": BrokerPosition("SPY", Decimal("10"), Decimal("130"), Decimal("1300"))}
    assert _adopt_carried_positions(_rec(entry_price="100"), broker) is None


def test_adopt_refuses_unrecorded_broker_symbol():
    broker = {"QQQ": BrokerPosition("QQQ", Decimal("5"), Decimal("50"), Decimal("250"))}
    assert _adopt_carried_positions(_rec(symbol="SPY"), broker) is None


def test_adopt_refuses_corrupt_field_cleanly():
    # An otherwise-matching record with a non-numeric stop_price must refuse
    # (return None), not raise an uncaught exception out of run_session.
    broker = {"SPY": BrokerPosition("SPY", Decimal("10"), Decimal("100"), Decimal("1000"))}
    assert _adopt_carried_positions(_rec(stop_price="not-a-number"), broker) is None
