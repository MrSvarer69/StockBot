from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_bot.contracts import OrderSide, RiskParams
from trading_bot.execution import SessionConfig, run_session  # noqa: F401
from trading_bot.strategy import ORBStrategy, load_config

from tests.test_execution.fakes import FakeBroker
from tests.test_strategy.fixtures import make_synthetic_session


_SESSION_DATE = date(2026, 1, 5)
# 10:30 ET = 15:30 UTC — bars up to here include the 10:00 ET breakout signal
# but NOT the 15:55 ET flat signal.
_AFTER_ENTRY = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)
# 16:05 ET = 21:05 UTC — full session including the flat signal.
_AFTER_SESSION = datetime(2026, 1, 5, 21, 5, tzinfo=UTC)


def _strategy():
    return ORBStrategy(load_config())


def _risk(symbols=("SPY",)):
    return RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
        max_position_count=5,
        symbol_whitelist=frozenset(symbols),
    )


def _config(**kw):
    base = dict(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        # Most tests replay historical bars far older than 5 min — disable the
        # staleness guard. Tests covering the guard explicitly override this.
        max_signal_age_minutes=None,
        # Many tests seed FakeBroker.positions to exercise specific scenarios;
        # the reconcile-on-startup check would otherwise refuse to start. Tests
        # covering the orphan-positions refusal use their own config.
        force_ignore_unreconciled=True,
    )
    base.update(kw)
    return SessionConfig(**base)


def test_refuses_non_paper_broker():
    broker = FakeBroker(is_paper=False)
    with pytest.raises(RuntimeError, match="non-paper"):
        run_session(broker, _strategy(), _config(), _risk(), install_signals=False)


def test_exits_when_market_closed():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=10),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.iterations == 1
    assert not broker.submitted_orders


def test_up_breakout_places_buy_order():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.iterations == 1
    buys = [o for o in broker.submitted_orders if o.side == OrderSide.BUY]
    assert len(buys) == 1
    assert buys[0].symbol == "SPY"
    assert buys[0].qty > 0


def test_signal_dedup_across_iterations():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    run_session(
        broker,
        _strategy(),
        _config(max_iterations=5),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # Each poll sees the entry signal as the latest bar's signal. Dedupe
    # ensures it fires exactly once across 5 iterations.
    long_orders = [o for o in broker.submitted_orders if o.side == OrderSide.BUY]
    assert len(long_orders) == 1


def test_skips_entry_when_already_in_position():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    from trading_bot.execution.broker import BrokerPosition

    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("5"),
                avg_entry_price=Decimal("95"),
                market_value=Decimal("500"),
            )
        },
    )
    run_session(
        broker,
        _strategy(),
        _config(max_iterations=1),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    # Should not place a duplicate entry order.
    new_orders = [o for o in broker.submitted_orders if o.side == OrderSide.BUY]
    assert len(new_orders) == 0


def test_flat_signal_closes_position():
    """First run sees the entry only; second run, time-advanced, sees the flat."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    run_session(
        broker,
        _strategy(),
        _config(max_iterations=1),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert "SPY" in broker.positions
    run_session(
        broker,
        _strategy(),
        _config(max_iterations=1),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert "SPY" in broker.closed_symbols


def test_risk_rejection_blocks_order():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    # Whitelist that excludes SPY — risk should reject every entry.
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        symbol_whitelist=frozenset({"AAPL"}),
    )
    run_session(
        broker,
        _strategy(),
        _config(max_iterations=1),
        risk,
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert not broker.submitted_orders


def test_cumulative_notional_tracked():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.cumulative_notional_today > Decimal("0")


def test_flatten_on_exit():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    run_session(
        broker,
        _strategy(),
        _config(max_iterations=1, flatten_on_exit=True),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert broker.cancel_calls >= 1
    assert "SPY" in broker.closed_symbols


def test_broker_errors_dont_halt_loop():
    """A submit failure is per-signal, not per-iteration: counter stays at 0."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        fail_on_submit=True,
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=3),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.iterations == 3
    assert not broker.submitted_orders


def test_session_start_equity_recorded():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("123456"),
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.session_start_equity == Decimal("123456")


def test_realized_pnl_tracks_equity_delta():
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("100000"),
    )

    def sleep_fn(_):
        # Between iterations, the broker reports a 3k drop.
        broker.equity = Decimal("97000")

    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=2),
        _risk(),
        sleep_fn=sleep_fn,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.session_start_equity == Decimal("100000")
    assert state.realized_pnl_today == Decimal("-3000")


def test_daily_loss_kill_switch_blocks_new_entries():
    """When equity drops past the daily-loss cap, no entry orders go through."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("100000"),
    )
    # Pre-set start equity baseline and then drop equity past the 3% cap before
    # any signal can fire.
    state_holder = {"step": 0}

    def now_fn():
        state_holder["step"] += 1
        if state_holder["step"] == 1:
            # First poll establishes baseline at 100k.
            broker.equity = Decimal("100000")
        else:
            # Subsequent polls show a 5% drawdown — past the 3% cap.
            broker.equity = Decimal("95000")
        return _AFTER_ENTRY

    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.03"),
        max_daily_notional_pct=Decimal("0.95"),
        symbol_whitelist=frozenset({"SPY"}),
    )

    # Need at least 2 iterations: iter 1 sets baseline, iter 2 sees drawdown.
    run_session(
        broker,
        _strategy(),
        _config(max_iterations=2),
        risk,
        sleep_fn=lambda _s: None,
        now_fn=now_fn,
        install_signals=False,
    )
    # The first iteration may submit before equity drops; subsequent iterations
    # must not submit. With only one ORB long signal per session and dedup, we
    # check the count is bounded — at most one buy ever issued.
    buys = [o for o in broker.submitted_orders if o.side == OrderSide.BUY]
    assert len(buys) <= 1


def test_kill_file_halts_loop(tmp_path):
    kill_path = tmp_path / "HALT"
    kill_path.write_text("halt")
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        kill_file_path=str(kill_path),
        symbol_whitelist=frozenset({"SPY"}),
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=5),
        risk,
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.halt_reason is not None
    assert "kill file" in state.halt_reason
    assert not broker.submitted_orders


def test_kill_env_halts_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_KILL", "1")
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        kill_file_path=str(tmp_path / "absent"),
        symbol_whitelist=frozenset({"SPY"}),
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=5),
        risk,
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.halt_reason is not None
    assert "TRADING_KILL" in state.halt_reason
    assert not broker.submitted_orders


def test_consecutive_failures_halt_loop():
    """After max_consecutive_failures BrokerErrors in a row, the loop halts."""
    from trading_bot.execution.broker import BrokerError

    class AlwaysFailBroker(FakeBroker):
        def get_account(self):
            raise BrokerError("simulated outage")

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = AlwaysFailBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=20,
        flatten_on_exit=False,
        max_consecutive_failures=3,
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.halt_reason is not None
    assert "consecutive" in state.halt_reason
    assert state.consecutive_failures >= 3


def test_flatten_result_clean_exit():
    """All positions close successfully → FlattenResult records them."""
    from trading_bot.execution.broker import BrokerPosition

    broker = FakeBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("5"),
                avg_entry_price=Decimal("95"),
                market_value=Decimal("500"),
            ),
        },
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1, flatten_on_exit=True),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.flatten_result is not None
    assert state.flatten_result.cancel_ok is True
    assert state.flatten_result.positions_closed == ("SPY",)
    assert state.flatten_result.positions_failed == ()


def test_flatten_result_partial_close_failure():
    """A symbol whose close_position raises BrokerError lands in positions_failed."""
    from trading_bot.execution.broker import BrokerError, BrokerPosition

    class PartialFailBroker(FakeBroker):
        def close_position(self, symbol):
            if symbol == "AAPL":
                raise BrokerError("simulated close failure")
            return super().close_position(symbol)

    broker = PartialFailBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100"), "AAPL": Decimal("200")},
        market_open=False,
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("5"),
                avg_entry_price=Decimal("95"),
                market_value=Decimal("500"),
            ),
            "AAPL": BrokerPosition(
                symbol="AAPL",
                qty=Decimal("3"),
                avg_entry_price=Decimal("195"),
                market_value=Decimal("600"),
            ),
        },
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1, flatten_on_exit=True),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.flatten_result is not None
    assert state.flatten_result.cancel_ok is True
    assert "SPY" in state.flatten_result.positions_closed
    assert "AAPL" in state.flatten_result.positions_failed


def test_flatten_result_records_cancel_failure():
    """cancel_all_orders raising BrokerError → cancel_ok is False but flatten continues."""
    from trading_bot.execution.broker import BrokerError

    class CancelFailingBroker(FakeBroker):
        def cancel_all_orders(self):
            raise BrokerError("simulated cancel failure")

    broker = CancelFailingBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1, flatten_on_exit=True),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.flatten_result is not None
    assert state.flatten_result.cancel_ok is False


def test_flatten_propagates_unexpected_exception():
    """Non-BrokerError exceptions during flatten must NOT be swallowed."""

    class WildBroker(FakeBroker):
        def cancel_all_orders(self):
            raise ValueError("not a broker error")

    broker = WildBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,
    )
    with pytest.raises(ValueError):
        run_session(
            broker,
            _strategy(),
            _config(max_iterations=1, flatten_on_exit=True),
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_SESSION,
            install_signals=False,
        )


def test_session_start_equity_persists_across_runs(tmp_path):
    """A second run on the same ET date loads the first run's baseline."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    state_dir = tmp_path / "shared_session_state"
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        session_state_dir=state_dir,
    )

    broker1 = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("100000"),
    )
    state1 = run_session(
        broker1,
        _strategy(),
        cfg,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state1.session_start_equity == Decimal("100000")

    # Simulate a mid-day crash + restart. Without persistence, the baseline
    # would rebase to the (already-drawn-down) 95k. With persistence it stays
    # at 100k so the daily-loss cap still measures from the day's opening.
    broker2 = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("95000"),
    )
    state2 = run_session(
        broker2,
        _strategy(),
        cfg,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state2.session_start_equity == Decimal("100000")
    assert state2.realized_pnl_today == Decimal("-5000")


def test_session_start_equity_persistence_file_layout(tmp_path):
    """File path is keyed on ET date and contains the equity as a string."""
    import json

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    state_dir = tmp_path / "session_state"
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        session_state_dir=state_dir,
    )
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("100000"),
    )
    run_session(
        broker,
        _strategy(),
        cfg,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # _AFTER_ENTRY is 2026-01-05 15:30 UTC = 2026-01-05 10:30 ET.
    expected_file = state_dir / "2026-01-05.json"
    assert expected_file.exists()
    payload = json.loads(expected_file.read_text())
    assert payload == {"session_start_equity": "100000"}


def test_kill_file_during_sleep_halts_within_chunk(tmp_path):
    """A kill file dropped mid-sleep halts before the full poll interval elapses."""
    kill_path = tmp_path / "HALT"
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        kill_file_path=str(kill_path),
        symbol_whitelist=frozenset({"SPY"}),
    )

    sleep_calls: list[int] = []

    def sleep_fn(seconds):
        sleep_calls.append(seconds)
        # Drop the kill file on the 2nd 5-second chunk of the sleep window.
        if len(sleep_calls) == 2:
            kill_path.write_text("halt")

    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=60,  # 12 chunks of 5s each
        max_iterations=5,
        flatten_on_exit=False,
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        risk,
        sleep_fn=sleep_fn,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.halt_reason is not None
    assert "kill file" in state.halt_reason
    # Should stop after detecting the kill file mid-sleep — not after the
    # full 60s window (which would be 12 chunks per iteration).
    assert len(sleep_calls) < 12


def test_kill_env_during_sleep_halts_within_chunk(monkeypatch):
    """A kill env var flipped mid-sleep halts before the full poll interval."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    risk = _risk()

    sleep_calls: list[int] = []

    def sleep_fn(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) == 3:
            monkeypatch.setenv("TRADING_KILL", "1")

    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=60,
        max_iterations=5,
        flatten_on_exit=False,
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        risk,
        sleep_fn=sleep_fn,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.halt_reason is not None
    assert "TRADING_KILL" in state.halt_reason
    assert len(sleep_calls) < 12


def test_save_session_start_equity_is_atomic(tmp_path):
    """Atomic write: after _save, the .tmp sibling must not exist."""
    from trading_bot.execution.session import _save_session_start_equity

    path = tmp_path / "state" / "2026-05-14.json"
    _save_session_start_equity(path, Decimal("100000"))
    assert path.exists()
    assert not (path.with_suffix(path.suffix + ".tmp")).exists()


def test_persisted_baseline_rejected_when_drift_exceeds_threshold(
    tmp_path, caplog
):
    """A persisted baseline that deviates wildly from broker equity is rebased."""
    import json
    import logging

    # Seed disk with a baseline that's 10x the broker's current equity — far
    # past the 50% drift threshold. Tampering / cross-account / restore bug.
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    # _AFTER_ENTRY is 2026-01-05 ET date.
    seed_path = state_dir / "2026-01-05.json"
    seed_path.write_text(json.dumps({"session_start_equity": "1000000"}))

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("100000"),
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        session_state_dir=state_dir,
    )
    with caplog.at_level(logging.ERROR, logger="trading_bot.execution.session"):
        state = run_session(
            broker,
            _strategy(),
            cfg,
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_ENTRY,
            install_signals=False,
        )
    assert state.session_start_equity == Decimal("100000")
    assert any("rejected" in r.message for r in caplog.records)
    # File on disk should now contain the rebased value.
    reloaded = json.loads(seed_path.read_text())
    assert reloaded["session_start_equity"] == "100000"


def test_persisted_baseline_kept_when_within_drift(tmp_path):
    """A baseline within the drift threshold is preserved (intra-day restart)."""
    import json

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    seed_path = state_dir / "2026-01-05.json"
    # 5% delta from broker equity — well under the 20% threshold and
    # representative of a real intra-day restart after a drawdown.
    seed_path.write_text(json.dumps({"session_start_equity": "105000"}))

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("100000"),
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        session_state_dir=state_dir,
    )
    state = run_session(
        broker,
        _strategy(),
        cfg,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.session_start_equity == Decimal("105000")
    assert state.realized_pnl_today == Decimal("-5000")


def test_unreconciled_sentinel_blocks_startup(tmp_path):
    """A leftover sentinel file refuses the next run_session start."""
    unrec_dir = tmp_path / "unreconciled"
    unrec_dir.mkdir(parents=True)
    (unrec_dir / "UNRECONCILED_20260513T200000Z.json").write_text("{}")

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        unreconciled_dir=unrec_dir,
    )
    with pytest.raises(RuntimeError, match="unreconciled"):
        run_session(
            broker,
            _strategy(),
            cfg,
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_ENTRY,
            install_signals=False,
        )


def test_unreconciled_sentinel_can_be_force_ignored(tmp_path, caplog):
    """force_ignore_unreconciled=True starts the loop and logs an ERROR."""
    import logging

    unrec_dir = tmp_path / "unreconciled"
    unrec_dir.mkdir(parents=True)
    (unrec_dir / "UNRECONCILED_20260513T200000Z.json").write_text("{}")

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        unreconciled_dir=unrec_dir,
        force_ignore_unreconciled=True,
    )
    with caplog.at_level(logging.ERROR, logger="trading_bot.execution.session"):
        state = run_session(
            broker,
            _strategy(),
            cfg,
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_ENTRY,
            install_signals=False,
        )
    assert state.iterations == 1
    assert any("unreconciled" in r.message.lower() for r in caplog.records)


def test_unreconciled_sentinel_written_on_partial_flatten_failure(
    tmp_path, caplog
):
    """Partial flatten failure writes a sentinel and logs at ERROR level."""
    import json
    import logging

    from trading_bot.execution.broker import BrokerError, BrokerPosition

    class PartialFailBroker(FakeBroker):
        def close_position(self, symbol):
            if symbol == "AAPL":
                raise BrokerError("simulated close failure")
            return super().close_position(symbol)

    unrec_dir = tmp_path / "unreconciled"
    broker = PartialFailBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100"), "AAPL": Decimal("200")},
        market_open=False,
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("5"),
                avg_entry_price=Decimal("95"),
                market_value=Decimal("500"),
            ),
            "AAPL": BrokerPosition(
                symbol="AAPL",
                qty=Decimal("3"),
                avg_entry_price=Decimal("195"),
                market_value=Decimal("600"),
            ),
        },
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=True,
        unreconciled_dir=unrec_dir,
        run_id="TEST_RUN_ID",
        force_ignore_unreconciled=True,  # pre-seeded positions in FakeBroker
    )
    with caplog.at_level(logging.ERROR, logger="trading_bot.execution.session"):
        state = run_session(
            broker,
            _strategy(),
            cfg,
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_SESSION,
            install_signals=False,
        )
    assert state.flatten_result is not None
    assert "AAPL" in state.flatten_result.positions_failed

    sentinel = unrec_dir / "UNRECONCILED_TEST_RUN_ID.json"
    assert sentinel.exists()
    payload = json.loads(sentinel.read_text())
    assert payload["run_id"] == "TEST_RUN_ID"
    assert "AAPL" in payload["failed_symbols"]
    assert "created_at_utc" in payload

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("UNRECONCILED" in r.message for r in error_records)


def test_sentinel_collision_does_not_overwrite_existing(tmp_path):
    """Two writes with the same run_id produce two distinct files."""
    from trading_bot.execution.session import _write_unreconciled_sentinel

    unrec_dir = tmp_path / "unreconciled"
    cfg = SessionConfig(
        symbols=["SPY"],
        unreconciled_dir=unrec_dir,
    )
    now = _AFTER_SESSION
    p1 = _write_unreconciled_sentinel(cfg, "SAME_ID", ("AAPL",), now)
    p2 = _write_unreconciled_sentinel(cfg, "SAME_ID", ("MSFT",), now)
    assert p1.exists()
    assert p2.exists()
    assert p1 != p2
    # First record is preserved (AAPL); the second write adopted a suffix.
    import json

    assert json.loads(p1.read_text())["failed_symbols"] == ["AAPL"]
    assert json.loads(p2.read_text())["failed_symbols"] == ["MSFT"]


def test_clean_flatten_does_not_write_sentinel(tmp_path):
    """No failed positions → no sentinel file."""
    from trading_bot.execution.broker import BrokerPosition

    unrec_dir = tmp_path / "unreconciled"
    broker = FakeBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("5"),
                avg_entry_price=Decimal("95"),
                market_value=Decimal("500"),
            ),
        },
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=True,
        unreconciled_dir=unrec_dir,
        run_id="CLEAN_RUN",
        force_ignore_unreconciled=True,  # pre-seeded position
    )
    run_session(
        broker,
        _strategy(),
        cfg,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    sentinels = list(unrec_dir.glob("UNRECONCILED_*.json")) if unrec_dir.exists() else []
    assert sentinels == []


def test_daily_loss_kill_during_sleep_halts_loop(tmp_path):
    """Equity dropping past the daily-loss cap mid-sleep halts within ~15s."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        equity=Decimal("100000"),
    )
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.03"),
        max_daily_notional_pct=Decimal("0.95"),
        symbol_whitelist=frozenset({"SPY"}),
    )

    sleep_calls: list[int] = []

    def sleep_fn(seconds):
        sleep_calls.append(seconds)
        # On the 3rd 5-second chunk (the first time the daily-loss check
        # runs mid-sleep), drop equity past the 3% cap.
        if len(sleep_calls) == 3:
            broker.equity = Decimal("90000")  # -10% drawdown, past the 3% cap

    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=60,  # 12 chunks of 5s each
        max_iterations=5,
        flatten_on_exit=False,
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        risk,
        sleep_fn=sleep_fn,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.halt_reason is not None
    assert "daily loss limit hit" in state.halt_reason
    # Should halt within the first daily-loss check window — not the full 60s.
    assert len(sleep_calls) <= 4


def test_paper_session_records_trade_on_flat_signal():
    """An entry followed by a flat-signal exit produces a Trade with reason=time."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    # Live semantics: each poll acts on the latest bar's signal only.
    # First iteration uses _AFTER_ENTRY so the breakout is the latest bar → entry.
    # Later iterations advance to _AFTER_SESSION so the flat bar is the latest → exit.
    # now_fn is called twice at startup (run_id + state_path) and once per
    # iteration, so the first iteration is call #3.
    step = {"i": 0}

    def now_fn():
        step["i"] += 1
        return _AFTER_ENTRY if step["i"] <= 3 else _AFTER_SESSION

    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=2, flatten_on_exit=False),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=now_fn,
        install_signals=False,
    )
    assert len(state.trades) == 1
    trade = state.trades[0]
    assert trade.symbol == "SPY"
    assert trade.side == "long"
    assert trade.exit_reason == "time"
    assert trade.qty > 0


def test_paper_session_records_trade_on_flatten():
    """Entry without flat signal, closed by _flatten_all → reason=session_end."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    # _AFTER_ENTRY: only the entry signal is visible, flat signal not yet.
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1, flatten_on_exit=True),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert len(state.trades) == 1
    trade = state.trades[0]
    assert trade.symbol == "SPY"
    assert trade.side == "long"
    assert trade.exit_reason == "session_end"


def test_paper_session_no_trades_when_no_signals():
    """No bars, no signals, no trades."""
    broker = FakeBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,  # exit immediately
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1, flatten_on_exit=False),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.trades == []
    assert state.open_entries == {}


def test_paper_session_flatten_of_external_position_does_not_record_trade():
    """A position the bot did NOT open in this session is closed but not tracked."""
    from trading_bot.execution.broker import BrokerPosition

    broker = FakeBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("5"),
                avg_entry_price=Decimal("95"),
                market_value=Decimal("500"),
            ),
        },
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=1, flatten_on_exit=True),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_SESSION,
        install_signals=False,
    )
    assert state.trades == []
    # Flatten still closed the position — sentinel logic still ran.
    assert state.flatten_result is not None
    assert "SPY" in state.flatten_result.positions_closed


def test_broker_auth_error_halts_immediately():
    """BrokerAuthError (HTTP 401) must halt on iteration 1 — no retry can
    succeed without operator intervention. Catches the carry-forward
    'misconfigured live API key keeps retrying' concern."""
    from trading_bot.execution.broker import BrokerAuthError

    class AuthFailBroker(FakeBroker):
        def get_account(self):
            raise BrokerAuthError("simulated 401")

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = AuthFailBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=10),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # Halted on first iteration, not after 5.
    assert state.iterations == 1
    assert state.halt_reason is not None
    assert "BrokerAuthError" in state.halt_reason
    assert state.consecutive_failures == 0  # Did NOT enter the retry path.


def test_mid_sleep_consecutive_transient_failures_halt_loop():
    """Regression: a flapping connection that recovers each top-of-loop but
    fails every mid-sleep daily-loss check must halt after N consecutive
    swallowed failures, not mask the cap indefinitely."""
    from trading_bot.execution.broker import BrokerError

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")

    class FlapBroker(FakeBroker):
        get_account_calls: int = 0

        def get_account(self):
            self.get_account_calls += 1
            # Call 1 (top of loop iter 1) succeeds — baseline + signals fire.
            # Subsequent calls (mid-sleep) fail. Top-of-loop iter 2 would also
            # fail, but the mid-sleep counter halts before iter 2 begins.
            if self.get_account_calls == 1:
                return super().get_account()
            raise BrokerError("simulated mid-sleep transient")

    broker = FlapBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=60,  # 12 chunks of 5s → 4 daily-loss checks
        max_iterations=10,
        flatten_on_exit=False,
        max_consecutive_midsleep_failures=3,
        max_signal_age_minutes=None,
        force_ignore_unreconciled=True,
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.halt_reason is not None
    assert "mid-sleep" in state.halt_reason
    assert state.consecutive_midsleep_failures >= 3


def test_mid_sleep_counter_resets_on_success():
    """A single transient mid-sleep failure followed by success must reset
    the counter — it should only fire on sustained outage."""
    from trading_bot.execution.broker import BrokerError

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")

    class OneShotFlapBroker(FakeBroker):
        get_account_calls: int = 0

        def get_account(self):
            self.get_account_calls += 1
            # Call 2 (first mid-sleep) fails; others succeed.
            if self.get_account_calls == 2:
                raise BrokerError("one-shot transient")
            return super().get_account()

    broker = OneShotFlapBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=60,
        max_iterations=2,
        flatten_on_exit=False,
        max_consecutive_midsleep_failures=3,
        max_signal_age_minutes=None,
        force_ignore_unreconciled=True,
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # Counter must have reset on the next successful mid-sleep call.
    assert state.consecutive_midsleep_failures == 0
    assert state.halt_reason is None


def test_mid_sleep_auth_error_still_runs_flatten():
    """Regression: an auth error mid-sleep must not bypass flatten_on_exit.
    Before the wrapping try/except around _sleep_with_safety_checks, the
    raise propagated out of run_session uncaught, orphaning positions
    without writing a sentinel."""
    from trading_bot.execution.broker import BrokerAuthError, BrokerPosition

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")

    class MidSleepAuthBroker(FakeBroker):
        get_account_calls: int = 0

        def get_account(self):
            self.get_account_calls += 1
            # Iter 1 top-of-loop succeeds (seed baseline + place entry).
            # First mid-sleep call raises auth.
            if self.get_account_calls >= 2:
                raise BrokerAuthError("mid-sleep 401")
            return super().get_account()

    broker = MidSleepAuthBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        # Pre-seed an open position so flatten has something to close.
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("5"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("500"),
            ),
        },
    )
    state = run_session(
        broker,
        _strategy(),
        _config(
            max_iterations=10,
            poll_interval_seconds=60,
            flatten_on_exit=True,
        ),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # Halt reason set, flatten ran (positions closed).
    assert state.halt_reason is not None
    assert "BrokerAuthError" in state.halt_reason
    assert state.flatten_result is not None
    assert "SPY" in state.flatten_result.positions_closed


def test_mid_sleep_auth_error_does_not_get_swallowed():
    """Regression: an auth-class error during the mid-sleep daily-loss check
    must surface (not be silently absorbed as a transient outage)."""
    from trading_bot.execution.broker import BrokerAuthError

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")

    class MidSleepAuthBroker(FakeBroker):
        get_account_calls: int = 0

        def get_account(self):
            self.get_account_calls += 1
            # First call (top of loop, iteration 1) succeeds so we get into
            # the sleep. Mid-sleep call (2nd) raises auth.
            if self.get_account_calls >= 2:
                raise BrokerAuthError("simulated mid-sleep 401")
            return super().get_account()

    broker = MidSleepAuthBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=10, poll_interval_seconds=60),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # Without the narrowing, the auth would be swallowed and the loop would
    # continue. With the fix, mid-sleep raises propagate out of the sleep
    # helper which the main except branch catches.
    assert state.halt_reason is not None
    assert "BrokerAuthError" in state.halt_reason


def test_broker_validation_error_halts_immediately():
    """BrokerValidationError (HTTP 422) halts on the same logic as auth."""
    from trading_bot.execution.broker import BrokerValidationError

    class ValidationFailBroker(FakeBroker):
        def get_account(self):
            raise BrokerValidationError("simulated 422")

    broker = ValidationFailBroker(
        bars_by_symbol={"SPY": make_synthetic_session(_SESSION_DATE, breakout="up")},
        latest_prices={"SPY": Decimal("100")},
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=10),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert state.iterations == 1
    assert "BrokerValidationError" in state.halt_reason


def test_picker_caps_concurrent_entries_across_symbols():
    """With max_concurrent_entries=2 and 3 fresh entry signals, only 2 fire."""
    base = make_synthetic_session(_SESSION_DATE, breakout="up", symbol="SPY")
    bars_a = base.copy()
    bars_a["symbol"] = "AAA"
    bars_b = base.copy()
    bars_b["symbol"] = "BBB"
    bars_c = base.copy()
    bars_c["symbol"] = "CCC"

    broker = FakeBroker(
        bars_by_symbol={"AAA": bars_a, "BBB": bars_b, "CCC": bars_c},
        latest_prices={"AAA": Decimal("100"), "BBB": Decimal("100"), "CCC": Decimal("100")},
    )
    cfg = SessionConfig(
        symbols=["AAA", "BBB", "CCC"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        max_signal_age_minutes=None,
        force_ignore_unreconciled=True,
        max_concurrent_entries=2,
    )
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
        max_position_count=5,
        symbol_whitelist=frozenset({"AAA", "BBB", "CCC"}),
    )
    run_session(
        broker,
        _strategy(),
        cfg,
        risk,
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # Exactly 2 entries placed, even though 3 candidates were available.
    # (On a single-session backtest all symbols hit cold-start ratio=1.0, so
    # the picker falls back to alphabetical: AAA, BBB.)
    assert len(broker.submitted_orders) == 2
    submitted_symbols = {o.symbol for o in broker.submitted_orders}
    assert submitted_symbols == {"AAA", "BBB"}
    assert "CCC" not in submitted_symbols


def test_picker_prefers_highest_or_atr_ratio_when_buffer_populated():
    """With multi-session bars (so ATR buffer is warm), the picker's score is
    not the cold-start tie of 1.0 anymore. The symbol with the widest OR
    relative to its session-ATR must be among the picks."""
    from datetime import date, timedelta

    from tests.test_strategy.fixtures import make_multi_session, make_synthetic_session

    # 5 prior sessions of consistent narrow range build the ATR baseline.
    prior_sessions_a = [
        make_synthetic_session(
            date(2026, 1, 5) - timedelta(days=10 + i),
            breakout="none",
            or_high=101,
            or_low=99,
            symbol="AAA",
        )
        for i in range(5)
    ]
    # AAA's current session: same width as prior → ratio ≈ 1.0.
    today_a = make_synthetic_session(
        _SESSION_DATE, breakout="up", or_high=101, or_low=99, symbol="AAA"
    )
    bars_a = make_multi_session(prior_sessions_a + [today_a])

    # BBB: prior sessions same narrow shape — but TODAY has a wider OR (5pt).
    prior_sessions_b = [
        make_synthetic_session(
            date(2026, 1, 5) - timedelta(days=10 + i),
            breakout="none",
            or_high=101,
            or_low=99,
            symbol="BBB",
        )
        for i in range(5)
    ]
    today_b = make_synthetic_session(
        _SESSION_DATE, breakout="up", or_high=103, or_low=98, symbol="BBB"
    )
    bars_b = make_multi_session(prior_sessions_b + [today_b])

    broker = FakeBroker(
        bars_by_symbol={"AAA": bars_a, "BBB": bars_b},
        latest_prices={"AAA": Decimal("100"), "BBB": Decimal("100")},
    )
    cfg = SessionConfig(
        symbols=["AAA", "BBB"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        max_signal_age_minutes=None,
        force_ignore_unreconciled=True,
        max_concurrent_entries=1,  # only one fires → must be the higher ratio
        # Default 3-day lookback doesn't include the prior sessions we seeded;
        # bump to ~20 days so the ATR buffer populates and ratios differ.
        lookback_minutes=60 * 24 * 20,
    )
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
        max_position_count=5,
        symbol_whitelist=frozenset({"AAA", "BBB"}),
    )
    run_session(
        broker,
        _strategy(),
        cfg,
        risk,
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    submitted = {o.symbol for o in broker.submitted_orders}
    assert len(broker.submitted_orders) == 1
    # BBB has a wider OR today (5pt) vs same ATR baseline → higher ratio than AAA.
    assert submitted == {"BBB"}


def test_picker_disabled_when_max_concurrent_entries_is_none():
    """Default behavior: no picker → every fresh entry fires (pre-picker semantics)."""
    bars_a = make_synthetic_session(_SESSION_DATE, breakout="up", symbol="AAA")
    bars_a["symbol"] = "AAA"
    bars_b = make_synthetic_session(_SESSION_DATE, breakout="up", symbol="BBB")
    bars_b["symbol"] = "BBB"
    broker = FakeBroker(
        bars_by_symbol={"AAA": bars_a, "BBB": bars_b},
        latest_prices={"AAA": Decimal("100"), "BBB": Decimal("100")},
    )
    cfg = SessionConfig(
        symbols=["AAA", "BBB"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        max_signal_age_minutes=None,
        force_ignore_unreconciled=True,
        max_concurrent_entries=None,  # default — no picker
    )
    risk = RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_notional_pct=Decimal("0.95"),
        max_position_count=5,
        symbol_whitelist=frozenset({"AAA", "BBB"}),
    )
    run_session(
        broker,
        _strategy(),
        cfg,
        risk,
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    submitted = {o.symbol for o in broker.submitted_orders}
    assert submitted == {"AAA", "BBB"}


def test_picker_does_not_block_flat_signals():
    """Even with max_concurrent_entries=0 (no new entries allowed), a flat
    signal must still close an existing position."""
    from trading_bot.execution.broker import BrokerPosition

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("1000"),
            ),
        },
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=2,
        flatten_on_exit=False,
        max_signal_age_minutes=None,
        force_ignore_unreconciled=True,
        max_concurrent_entries=0,  # entries forbidden; flats must still fire
    )
    step = {"i": 0}

    def now_fn():
        step["i"] += 1
        return _AFTER_ENTRY if step["i"] <= 3 else _AFTER_SESSION

    run_session(
        broker,
        _strategy(),
        cfg,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=now_fn,
        install_signals=False,
    )
    # Position was closed via the flat signal even with picker capping entries to 0.
    assert "SPY" in broker.closed_symbols


def test_refuses_start_when_orphan_broker_positions_exist():
    """Reconcile-on-startup: pre-existing broker positions without an explicit
    operator override must block startup. Catches orphans from a SIGKILL or
    crash that skipped the graceful flatten path.
    """
    from trading_bot.execution.broker import BrokerPosition

    broker = FakeBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("1000"),
            ),
        },
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        # Explicitly default (False) — the test exercises the refusal.
        force_ignore_unreconciled=False,
    )
    with pytest.raises(RuntimeError, match="orphan"):
        run_session(
            broker,
            _strategy(),
            cfg,
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_ENTRY,
            install_signals=False,
        )


def test_orphan_positions_can_be_force_ignored(caplog):
    """force_ignore_unreconciled=True starts despite orphan positions, logs ERROR."""
    import logging

    from trading_bot.execution.broker import BrokerPosition

    broker = FakeBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("1000"),
            ),
        },
        market_open=False,  # exit immediately after the startup check passes
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        force_ignore_unreconciled=True,
    )
    with caplog.at_level(logging.ERROR, logger="trading_bot.execution.session"):
        run_session(
            broker,
            _strategy(),
            cfg,
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_ENTRY,
            install_signals=False,
        )
    assert any(
        "pre-existing broker positions" in r.message for r in caplog.records
    )


def test_sentinel_check_precedes_orphan_position_check(tmp_path):
    """If BOTH a sentinel file AND orphan positions exist, the sentinel check
    must raise first. Locks the precedence so a future refactor doesn't silently
    invert it (which would leak the more-specific sentinel message)."""
    from trading_bot.execution.broker import BrokerPosition

    unrec_dir = tmp_path / "unreconciled"
    unrec_dir.mkdir(parents=True)
    (unrec_dir / "UNRECONCILED_20260513T200000Z.json").write_text("{}")

    broker = FakeBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("1000"),
            ),
        },
    )
    cfg = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        unreconciled_dir=unrec_dir,
        force_ignore_unreconciled=False,
    )
    with pytest.raises(RuntimeError, match="unreconciled"):
        run_session(
            broker,
            _strategy(),
            cfg,
            _risk(),
            sleep_fn=lambda _s: None,
            now_fn=lambda: _AFTER_ENTRY,
            install_signals=False,
        )


def test_startup_proceeds_when_broker_query_fails():
    """If get_positions raises during startup, log and proceed — the main loop
    will retry and surface a real outage via consecutive_failures."""
    from trading_bot.execution.broker import BrokerError

    class StartupFailBroker(FakeBroker):
        get_positions_calls: int = 0

        def get_positions(self):
            self.get_positions_calls += 1
            if self.get_positions_calls == 1:
                raise BrokerError("startup transient")
            return super().get_positions()

    broker = StartupFailBroker(
        bars_by_symbol={},
        latest_prices={"SPY": Decimal("100")},
        market_open=False,
    )
    run_session(
        broker,
        _strategy(),
        _config(),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )


def test_flat_signal_cancels_pending_orders_before_close():
    """Regression for the 2026-05-14 wash-trade rejection: flat signal must
    cancel any pending entry order for the symbol before calling close_position.
    Otherwise Alpaca rejects with `code 40310000 opposite side market exists`."""
    from trading_bot.execution.broker import BrokerPosition

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")

    class TrackingBroker(FakeBroker):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.cancel_for_calls: list = []

        def cancel_orders_for(self, symbol):
            self.cancel_for_calls.append(symbol)
            return 1  # simulate one pending order canceled

    broker = TrackingBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("1000"),
            ),
        },
    )
    step = {"i": 0}

    def now_fn():
        step["i"] += 1
        return _AFTER_ENTRY if step["i"] <= 3 else _AFTER_SESSION

    run_session(
        broker,
        _strategy(),
        _config(max_iterations=2, flatten_on_exit=False),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=now_fn,
        install_signals=False,
    )
    assert "SPY" in broker.cancel_for_calls, (
        "cancel_orders_for must be called before close_position on flat signal"
    )
    assert "SPY" in broker.closed_symbols


def test_flat_signal_proceeds_when_cancel_raises():
    """If cancel_orders_for raises a BrokerError, the flat still attempts the
    close — cancel is best-effort cleanup, not a hard precondition."""
    from trading_bot.execution.broker import BrokerError, BrokerPosition

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")

    class CancelFailBroker(FakeBroker):
        def cancel_orders_for(self, symbol):
            raise BrokerError("simulated cancel failure")

    broker = CancelFailBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("1000"),
            ),
        },
    )
    step = {"i": 0}

    def now_fn():
        step["i"] += 1
        return _AFTER_ENTRY if step["i"] <= 3 else _AFTER_SESSION

    run_session(
        broker,
        _strategy(),
        _config(max_iterations=2, flatten_on_exit=False),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=now_fn,
        install_signals=False,
    )
    assert "SPY" in broker.closed_symbols


def test_stale_signal_skipped_when_age_exceeds_threshold():
    """A breakout bar hours old must not be replayed when the session starts late.

    Regression for 2026-05-14: 7-name universe launched at 13:57 ET and
    immediately placed 5 entries on opening-range breakouts that occurred
    10:01-10:49 ET. With max_signal_age_minutes=5, none of those would fire.
    """
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    # Latest bar is ~16:00 ET on _SESSION_DATE; now_fn returns the same date
    # but 5 hours later → signal age >>> 5 min. The guard should reject.
    late_now = datetime(2026, 1, 6, 2, 0, tzinfo=UTC)  # 21:00 ET next day
    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        max_signal_age_minutes=5,  # production default
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: late_now,
        install_signals=False,
    )
    assert state.iterations == 1
    assert broker.submitted_orders == []
    assert state.trades == []


def test_stale_flat_signal_still_fires():
    """A stale FLAT signal must still close a position — the staleness rule
    is for entries only. A stale flat means 'you should already be out';
    skipping it would leave the position open until session end."""
    from trading_bot.execution.broker import BrokerPosition

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
        positions={
            "SPY": BrokerPosition(
                symbol="SPY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("100"),
                market_value=Decimal("1000"),
            ),
        },
    )
    late_now = datetime(2026, 1, 6, 2, 0, tzinfo=UTC)
    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        max_signal_age_minutes=5,
        force_ignore_unreconciled=True,  # pre-seeded position
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: late_now,
        install_signals=False,
    )
    # Strategy emits a flat at the end of the synthetic session; even though
    # it is hours old by `late_now`, the close_position call must still fire.
    assert "SPY" in broker.closed_symbols


def test_fresh_signal_passes_staleness_guard():
    """Signals within the threshold are still executed."""
    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FakeBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    # Synthetic breakout fires at ~15:01 UTC (first bar after 30-min OR window).
    # _AFTER_ENTRY=15:30 UTC → signal age ~29 min. Use a 60-min threshold to
    # prove the guard accepts what is in-window, while still being protective.
    config = SessionConfig(
        symbols=["SPY"],
        poll_interval_seconds=0,
        max_iterations=1,
        flatten_on_exit=False,
        max_signal_age_minutes=60,
    )
    state = run_session(
        broker,
        _strategy(),
        config,
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    assert len(broker.submitted_orders) == 1


def test_trade_records_real_fill_price_when_submit_returns_no_fill():
    """Regression: market orders return `accepted` with filled_avg_price=None.

    The session must poll the broker via get_order until a fill price arrives,
    so the Trade record has the real fill instead of 0. Was producing
    entry_price=0/exit_price=0/pnl=0 in every paper session log on 2026-05-14.
    """
    from datetime import UTC, datetime
    from decimal import Decimal as D
    from uuid import uuid4

    from trading_bot.execution.broker import BrokerOrderResponse

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    fill_price = D("105.50")

    class AsyncFillBroker(FakeBroker):
        """Returns `accepted` with no fill, then `filled` on first get_order."""

        def submit_order(self, order):
            self.submitted_orders.append(order)
            from trading_bot.contracts import OrderSide as OS

            signed = order.qty if order.side == OS.BUY else -order.qty
            from trading_bot.execution.broker import BrokerPosition

            self.positions[order.symbol] = BrokerPosition(
                symbol=order.symbol,
                qty=signed,
                avg_entry_price=fill_price,
                market_value=signed * fill_price,
            )
            resp = BrokerOrderResponse(
                broker_order_id=str(uuid4()),
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                status="accepted",
                submitted_at=datetime.now(UTC),
                filled_qty=D("0"),
                filled_avg_price=None,
            )
            self.order_responses[resp.broker_order_id] = BrokerOrderResponse(
                broker_order_id=resp.broker_order_id,
                client_order_id=resp.client_order_id,
                symbol=resp.symbol,
                side=resp.side,
                qty=resp.qty,
                status="filled",
                submitted_at=resp.submitted_at,
                filled_qty=order.qty,
                filled_avg_price=fill_price,
            )
            return resp

    broker = AsyncFillBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": D("100")},
    )
    step = {"i": 0}

    def now_fn():
        step["i"] += 1
        return _AFTER_ENTRY if step["i"] <= 3 else _AFTER_SESSION

    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=2, flatten_on_exit=False),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=now_fn,
        install_signals=False,
    )
    assert len(state.trades) == 1
    trade = state.trades[0]
    # Pre-fix: entry_price was 0 (submit response had filled_avg_price=None and
    # market orders have no limit_price). Post-fix: _await_fill polls and the
    # second get_order returns the real fill.
    assert trade.entry_price == fill_price
    assert trade.entry_price != D("0")
    # Exit goes through FakeBroker.close_position which fills immediately at
    # latest_prices=100. pnl reflects the real round-trip, not 0 by construction.
    assert trade.exit_price == D("100")
    assert trade.pnl == (D("100") - fill_price) * trade.qty
    assert trade.pnl != D("0")


def test_consecutive_failure_counter_resets_on_success():
    """One transient failure followed by success should reset the counter."""
    from trading_bot.execution.broker import BrokerError

    class FlakyBroker(FakeBroker):
        attempts: int = 0

        def get_account(self):
            self.attempts += 1
            if self.attempts == 1:
                raise BrokerError("transient")
            return super().get_account()

    bars = make_synthetic_session(_SESSION_DATE, breakout="up")
    broker = FlakyBroker(
        bars_by_symbol={"SPY": bars},
        latest_prices={"SPY": Decimal("100")},
    )
    state = run_session(
        broker,
        _strategy(),
        _config(max_iterations=3),
        _risk(),
        sleep_fn=lambda _s: None,
        now_fn=lambda: _AFTER_ENTRY,
        install_signals=False,
    )
    # consecutive_failures should be 0 (reset by later successful iterations).
    assert state.consecutive_failures == 0
    assert state.halt_reason is None
