from __future__ import annotations

from decimal import Decimal

from trading_bot.risk import (
    AccountState,
    OrderSide,
    OrderType,
    Position,
    ProposedOrder,
    RiskParams,
    validate_order,
)


def _state(**overrides) -> AccountState:
    base = dict(equity=Decimal("100000"), cash=Decimal("100000"))
    base.update(overrides)
    return AccountState(**base)


def _params(**overrides) -> RiskParams:
    return RiskParams(**overrides)


def _order(**overrides) -> ProposedOrder:
    base = dict(
        symbol="SPY",
        side=OrderSide.BUY,
        qty=Decimal("10"),
        order_type=OrderType.MARKET,
    )
    base.update(overrides)
    return ProposedOrder(**base)


def test_happy_path_approved(tmp_path):
    params = _params(kill_file_path=str(tmp_path / "KILL"))
    decision = validate_order(_order(), _state(), params, market_is_open=True)
    assert decision.approved
    assert decision.order is not None


def test_connection_down_rejected(tmp_path):
    params = _params(kill_file_path=str(tmp_path / "KILL"))
    decision = validate_order(
        _order(), _state(connection_ok=False), params, market_is_open=True
    )
    assert not decision.approved
    assert "connection" in decision.reason


def test_zero_qty_rejected(tmp_path):
    params = _params(kill_file_path=str(tmp_path / "KILL"))
    decision = validate_order(
        _order(qty=Decimal("0")), _state(), params, market_is_open=True
    )
    assert not decision.approved
    assert "qty" in decision.reason


def test_negative_limit_price_rejected(tmp_path):
    params = _params(kill_file_path=str(tmp_path / "KILL"))
    decision = validate_order(
        _order(limit_price=Decimal("-1")), _state(), params, market_is_open=True
    )
    assert not decision.approved
    assert "limit_price" in decision.reason


def test_whitelist_blocks_unlisted_symbol(tmp_path):
    params = _params(
        symbol_whitelist=frozenset({"AAPL", "MSFT"}),
        kill_file_path=str(tmp_path / "KILL"),
    )
    decision = validate_order(_order(symbol="SPY"), _state(), params, market_is_open=True)
    assert not decision.approved
    assert "whitelist" in decision.reason


def test_empty_whitelist_allows_any(tmp_path):
    params = _params(kill_file_path=str(tmp_path / "KILL"))
    decision = validate_order(_order(symbol="TSLA"), _state(), params, market_is_open=True)
    assert decision.approved


def test_open_order_cap_rejected(tmp_path):
    params = _params(max_open_orders=2, kill_file_path=str(tmp_path / "KILL"))
    decision = validate_order(
        _order(), _state(open_order_count=2), params, market_is_open=True
    )
    assert not decision.approved
    assert "open orders" in decision.reason


def test_position_count_cap_rejected(tmp_path):
    params = _params(max_position_count=2, kill_file_path=str(tmp_path / "KILL"))
    positions = {
        "AAPL": Position("AAPL", Decimal("1"), Decimal("100")),
        "MSFT": Position("MSFT", Decimal("1"), Decimal("100")),
    }
    decision = validate_order(
        _order(symbol="TSLA"),
        _state(open_positions=positions),
        params,
        market_is_open=True,
    )
    assert not decision.approved
    assert "position count" in decision.reason


def test_adding_to_existing_position_allowed_even_at_cap(tmp_path):
    params = _params(max_position_count=2, kill_file_path=str(tmp_path / "KILL"))
    positions = {
        "SPY": Position("SPY", Decimal("1"), Decimal("100")),
        "AAPL": Position("AAPL", Decimal("1"), Decimal("100")),
    }
    decision = validate_order(
        _order(symbol="SPY"),
        _state(open_positions=positions),
        params,
        market_is_open=True,
    )
    assert decision.approved


def test_market_closed_rejected(tmp_path):
    params = _params(kill_file_path=str(tmp_path / "KILL"))
    decision = validate_order(_order(), _state(), params, market_is_open=False)
    assert not decision.approved
    assert "market closed" in decision.reason


def test_daily_loss_kill_switch_rejected(tmp_path):
    params = _params(
        max_daily_loss_pct=Decimal("0.03"),
        kill_file_path=str(tmp_path / "KILL"),
    )
    state = _state(realized_pnl_today=Decimal("-5000"))  # > 3% of 100k
    decision = validate_order(_order(), state, params, market_is_open=True)
    assert not decision.approved
    assert "daily loss" in decision.reason


def test_kill_file_rejected(tmp_path):
    kill_path = tmp_path / "KILL"
    kill_path.write_text("halt")
    params = _params(kill_file_path=str(kill_path))
    decision = validate_order(_order(), _state(), params, market_is_open=True)
    assert not decision.approved
    assert "kill file" in decision.reason


def test_kill_env_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_KILL", "1")
    params = _params(kill_file_path=str(tmp_path / "absent"))
    decision = validate_order(_order(), _state(), params, market_is_open=True)
    assert not decision.approved
    assert "TRADING_KILL" in decision.reason


def test_daily_notional_cap_blocks_oversize(tmp_path):
    params = _params(
        max_daily_notional_pct=Decimal("0.10"),
        kill_file_path=str(tmp_path / "absent"),
    )
    state = _state(cumulative_notional_today=Decimal("9500"))
    # equity 100k * 0.10 cap = 10000. Already used 9500. Order: 10 shares * $100 = $1000 → 10500 > 10000.
    decision = validate_order(
        _order(qty=Decimal("10")),
        state,
        params,
        market_is_open=True,
        current_price=Decimal("100"),
    )
    assert not decision.approved
    assert "daily notional cap" in decision.reason


def test_daily_notional_cap_allows_under_limit(tmp_path):
    params = _params(
        max_daily_notional_pct=Decimal("0.10"),
        kill_file_path=str(tmp_path / "absent"),
    )
    state = _state(cumulative_notional_today=Decimal("5000"))
    decision = validate_order(
        _order(qty=Decimal("10")),
        state,
        params,
        market_is_open=True,
        current_price=Decimal("100"),
    )
    assert decision.approved


def test_daily_notional_cap_skipped_without_price(tmp_path, caplog):
    """No current_price + no limit_price means we cannot enforce — log + allow."""
    params = _params(kill_file_path=str(tmp_path / "absent"))
    state = _state(cumulative_notional_today=Decimal("99999999"))
    decision = validate_order(_order(), state, params, market_is_open=True)
    # Approved because we cannot compute notional without a price.
    assert decision.approved


def test_daily_notional_uses_limit_price_when_market_price_absent(tmp_path):
    params = _params(
        max_daily_notional_pct=Decimal("0.10"),
        kill_file_path=str(tmp_path / "absent"),
    )
    state = _state(cumulative_notional_today=Decimal("9500"))
    decision = validate_order(
        _order(qty=Decimal("10"), limit_price=Decimal("100")),
        state,
        params,
        market_is_open=True,
    )
    assert not decision.approved
    assert "daily notional cap" in decision.reason


# ---------------------------------------------------------------------------
# max_capital_usd tightens the daily-notional cap inside validate_order.
# ---------------------------------------------------------------------------


def test_daily_notional_cap_uncapped_allows_order(tmp_path):
    # No max_capital_usd; daily cap = 0.50 * 10000 = $5000. A $250 order fits.
    params = _params(
        max_daily_notional_pct=Decimal("0.50"),
        max_capital_usd=None,
        kill_file_path=str(tmp_path / "absent"),
    )
    state = _state(cumulative_notional_today=Decimal("0"))
    decision = validate_order(
        _order(qty=Decimal("25")),
        state,
        params,
        market_is_open=True,
        current_price=Decimal("10"),
    )
    assert decision.approved


def test_daily_notional_cap_capped_fits_exactly(tmp_path):
    # max_capital_usd=$500 -> daily cap = 0.50 * 500 = $250.
    # A single $250 order should fit exactly (boundary is `>`).
    params = _params(
        max_daily_notional_pct=Decimal("0.50"),
        max_capital_usd=Decimal("500"),
        kill_file_path=str(tmp_path / "absent"),
    )
    state = _state(cumulative_notional_today=Decimal("0"))
    decision = validate_order(
        _order(qty=Decimal("25")),
        state,
        params,
        market_is_open=True,
        current_price=Decimal("10"),
    )
    assert decision.approved


def test_daily_notional_cap_capped_rejects_over_threshold(tmp_path):
    # Same cap as above; a $251 order spills past the $250 capped ceiling.
    params = _params(
        max_daily_notional_pct=Decimal("0.50"),
        max_capital_usd=Decimal("500"),
        kill_file_path=str(tmp_path / "absent"),
    )
    state = _state(cumulative_notional_today=Decimal("0"))
    decision = validate_order(
        _order(qty=Decimal("251")),
        state,
        params,
        market_is_open=True,
        current_price=Decimal("1"),
    )
    assert not decision.approved
    assert "daily notional cap hit" in decision.reason


# ---------------------------------------------------------------------------
# max_capital_usd is ALSO a hard total-exposure ceiling: open-position
# exposure (cost basis) + the proposed order must not exceed the cap. This is
# the check that was missing — the bug report showed ~$2k positions opened
# against a $750 cap because only the percentage caps existed.
# ---------------------------------------------------------------------------


def test_capital_cap_order_alone_exceeds_ceiling_rejected(tmp_path):
    """The original bug: a single entry worth more than the whole cap. With
    max_capital_usd=$750 a 60-share @ $33 (~$1,990) entry must be rejected
    by the capital ceiling (which is checked before the daily-flow cap)."""
    params = _params(
        max_capital_usd=Decimal("750"),
        max_position_count=10,
        kill_file_path=str(tmp_path / "absent"),
    )
    decision = validate_order(
        _order(symbol="NSP", qty=Decimal("60")),
        _state(),
        params,
        market_is_open=True,
        current_price=Decimal("33.18"),
    )
    assert not decision.approved
    assert "capital cap" in decision.reason


def test_capital_cap_blocks_when_existing_plus_order_exceeds(tmp_path):
    """$700 already deployed (cost basis) + a $100 new order = $800 > $750."""
    params = _params(
        max_capital_usd=Decimal("750"),
        max_position_count=10,
        kill_file_path=str(tmp_path / "absent"),
    )
    positions = {"AAPL": Position("AAPL", Decimal("7"), Decimal("100"))}  # $700
    decision = validate_order(
        _order(symbol="TSLA", qty=Decimal("10")),
        _state(open_positions=positions),
        params,
        market_is_open=True,
        current_price=Decimal("10"),  # $100 order
    )
    assert not decision.approved
    assert "capital cap" in decision.reason


def test_capital_cap_allows_when_under_ceiling(tmp_path):
    """$500 deployed + a $200 order = $700 <= $750, and $200 daily flow is
    under the 0.50*750=$375 daily cap → approved."""
    params = _params(
        max_capital_usd=Decimal("750"),
        max_position_count=10,
        kill_file_path=str(tmp_path / "absent"),
    )
    positions = {"AAPL": Position("AAPL", Decimal("5"), Decimal("100"))}  # $500
    decision = validate_order(
        _order(symbol="TSLA", qty=Decimal("20")),
        _state(open_positions=positions),
        params,
        market_is_open=True,
        current_price=Decimal("10"),  # $200 order
    )
    assert decision.approved


def test_capital_cap_boundary_exactly_at_ceiling_allowed(tmp_path):
    """$650 + $100 = $750 exactly; the comparison is strict `>` so it fits."""
    params = _params(
        max_capital_usd=Decimal("750"),
        max_daily_notional_pct=Decimal("1.0"),  # don't let daily cap interfere
        max_position_count=10,
        kill_file_path=str(tmp_path / "absent"),
    )
    positions = {"AAPL": Position("AAPL", Decimal("6.5"), Decimal("100"))}  # $650
    decision = validate_order(
        _order(symbol="TSLA", qty=Decimal("10")),
        _state(open_positions=positions),
        params,
        market_is_open=True,
        current_price=Decimal("10"),  # $100 order
    )
    assert decision.approved


def test_capital_cap_excludes_own_symbol_from_existing_exposure(tmp_path):
    """When the order's own symbol is already held, its existing exposure is
    excluded so the add path is not double-counted against the cap."""
    params = _params(
        max_capital_usd=Decimal("750"),
        max_position_count=10,
        kill_file_path=str(tmp_path / "absent"),
    )
    # SPY alone is $700; if it were counted, SPY + $100 order = $800 > $750.
    positions = {"SPY": Position("SPY", Decimal("7"), Decimal("100"))}
    decision = validate_order(
        _order(symbol="SPY", qty=Decimal("10")),
        _state(open_positions=positions),
        params,
        market_is_open=True,
        current_price=Decimal("10"),  # $100 order
    )
    assert decision.approved


def test_capital_cap_none_disables_ceiling(tmp_path):
    """No cap configured → unbounded total exposure permitted (legacy default)."""
    params = _params(
        max_capital_usd=None,
        max_position_count=10,
        kill_file_path=str(tmp_path / "absent"),
    )
    positions = {"AAPL": Position("AAPL", Decimal("1000"), Decimal("100"))}  # $100k
    decision = validate_order(
        _order(symbol="TSLA", qty=Decimal("10")),
        _state(open_positions=positions),
        params,
        market_is_open=True,
        current_price=Decimal("10"),
    )
    assert decision.approved
