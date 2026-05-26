from __future__ import annotations

from decimal import Decimal

from trading_bot.risk import (
    AccountState,
    RiskParams,
    check_daily_loss,
    check_kill_env,
    check_kill_file,
    kill_switches_ok,
)


def _state(**kw) -> AccountState:
    base = dict(equity=Decimal("100000"), cash=Decimal("100000"))
    base.update(kw)
    return AccountState(**base)


def test_daily_loss_within_limit_ok():
    params = RiskParams(max_daily_loss_pct=Decimal("0.03"))
    state = _state(realized_pnl_today=Decimal("-2000"))
    ok, _ = check_daily_loss(state, params)
    assert ok


def test_daily_loss_at_limit_trips():
    params = RiskParams(max_daily_loss_pct=Decimal("0.03"))
    state = _state(realized_pnl_today=Decimal("-3000"))
    ok, reason = check_daily_loss(state, params)
    assert not ok
    assert "daily loss" in reason


def test_daily_loss_beyond_limit_trips():
    params = RiskParams(max_daily_loss_pct=Decimal("0.03"))
    state = _state(realized_pnl_today=Decimal("-5000"))
    ok, _ = check_daily_loss(state, params)
    assert not ok


def test_kill_file_absent_ok(tmp_path):
    params = RiskParams(kill_file_path=str(tmp_path / "absent"))
    ok, _ = check_kill_file(params)
    assert ok


def test_kill_file_present_trips(tmp_path):
    kill = tmp_path / "KILL"
    kill.write_text("halt")
    params = RiskParams(kill_file_path=str(kill))
    ok, reason = check_kill_file(params)
    assert not ok
    assert "kill file" in reason


def test_aggregate_returns_first_failure(tmp_path):
    # Connection bad AND kill file present — connection should be reported first.
    kill = tmp_path / "KILL"
    kill.write_text("halt")
    params = RiskParams(kill_file_path=str(kill))
    state = _state(connection_ok=False)
    ok, reason = kill_switches_ok(state, params)
    assert not ok
    assert "connection" in reason


def test_aggregate_all_clear(tmp_path):
    params = RiskParams(kill_file_path=str(tmp_path / "absent"))
    ok, reason = kill_switches_ok(_state(), params)
    assert ok
    assert reason == "ok"


def test_kill_env_unset_ok(monkeypatch):
    monkeypatch.delenv("TRADING_KILL", raising=False)
    ok, _ = check_kill_env(RiskParams())
    assert ok


def test_kill_env_truthy_trips(monkeypatch):
    monkeypatch.setenv("TRADING_KILL", "1")
    ok, reason = check_kill_env(RiskParams())
    assert not ok
    assert "TRADING_KILL" in reason


def test_kill_env_case_insensitive_yes(monkeypatch):
    monkeypatch.setenv("TRADING_KILL", "Yes")
    ok, _ = check_kill_env(RiskParams())
    assert not ok


def test_kill_env_zero_is_ok(monkeypatch):
    monkeypatch.setenv("TRADING_KILL", "0")
    ok, _ = check_kill_env(RiskParams())
    assert ok


def test_kill_env_custom_var(monkeypatch):
    monkeypatch.setenv("MY_HALT", "true")
    ok, _ = check_kill_env(RiskParams(kill_env_var="MY_HALT"))
    assert not ok


# ---------------------------------------------------------------------------
# max_capital_usd tightens the daily-loss threshold.
# ---------------------------------------------------------------------------


def test_daily_loss_no_cap_does_not_trip_on_small_drawdown():
    # 3% of $10k = $300 threshold; a $20 loss is well within it.
    params = RiskParams(
        max_daily_loss_pct=Decimal("0.03"),
        max_capital_usd=None,
    )
    state = _state(equity=Decimal("10000"), realized_pnl_today=Decimal("-20"))
    ok, _ = check_daily_loss(state, params)
    assert ok


def test_daily_loss_cap_tightens_threshold_and_trips():
    # cap = $500 -> 3% threshold becomes $15; $20 loss now trips.
    params = RiskParams(
        max_daily_loss_pct=Decimal("0.03"),
        max_capital_usd=Decimal("500"),
    )
    state = _state(equity=Decimal("10000"), realized_pnl_today=Decimal("-20"))
    ok, reason = check_daily_loss(state, params)
    assert not ok
    assert "daily loss" in reason
