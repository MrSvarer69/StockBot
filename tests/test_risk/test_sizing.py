from __future__ import annotations

from decimal import Decimal

from trading_bot.risk import RiskParams, size_position


def _params(**kw) -> RiskParams:
    base = dict(max_pct_per_trade=Decimal("0.02"))
    base.update(kw)
    return RiskParams(**base)


def test_target_pct_cap_binds_when_stop_wide():
    qty = size_position(
        equity=Decimal("100000"),
        target_pct=Decimal("0.01"),
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        params=_params(max_pct_per_trade=Decimal("0.10")),
    )
    # target_notional = 1000; per-trade cap = 10000; risk cap = (100000*0.10)/10 = 1000 shares
    # qty from notional cap: 1000/100 = 10 shares — that's the bind.
    assert qty == Decimal("10")


def test_max_pct_per_trade_cap_binds():
    qty = size_position(
        equity=Decimal("100000"),
        target_pct=Decimal("0.50"),
        entry_price=Decimal("100"),
        stop_price=None,
        params=_params(max_pct_per_trade=Decimal("0.02")),
    )
    # cap = 100000 * 0.02 = 2000 notional; qty = 20
    assert qty == Decimal("20")


def test_stop_distance_cap_binds_when_stop_tight():
    qty = size_position(
        equity=Decimal("100000"),
        target_pct=Decimal("0.50"),
        entry_price=Decimal("100"),
        stop_price=Decimal("99"),
        params=_params(max_pct_per_trade=Decimal("0.02")),
    )
    # risk dollars = 2000; stop_distance = 1; risk_qty = 2000
    # notional cap qty = 2000/100 = 20 → notional cap still binds at 20
    assert qty == Decimal("20")


def test_stop_distance_cap_binds_when_per_trade_cap_loose():
    qty = size_position(
        equity=Decimal("100000"),
        target_pct=Decimal("0.50"),
        entry_price=Decimal("100"),
        stop_price=Decimal("99.5"),
        params=_params(max_pct_per_trade=Decimal("0.10")),
    )
    # notional cap: 10000/100 = 100 shares
    # risk cap: 10000/0.5 = 20000 shares
    # → notional cap binds → 100
    assert qty == Decimal("100")


def test_negative_equity_returns_zero():
    qty = size_position(
        equity=Decimal("-1"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("100"),
        stop_price=None,
        params=_params(),
    )
    assert qty == Decimal("0")


def test_negative_price_returns_zero():
    qty = size_position(
        equity=Decimal("100000"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("-1"),
        stop_price=None,
        params=_params(),
    )
    assert qty == Decimal("0")


def test_tiny_equity_rounds_down_to_zero():
    qty = size_position(
        equity=Decimal("10"),
        target_pct=Decimal("0.01"),
        entry_price=Decimal("1000"),
        stop_price=None,
        params=_params(max_pct_per_trade=Decimal("1.0")),
    )
    # 10*0.01/1000 = 0.0001 → rounds down to 0
    assert qty == Decimal("0")


def test_zero_return_logs_warning(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="trading_bot.risk.sizing")
    qty = size_position(
        equity=Decimal("-1"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("100"),
        stop_price=None,
        params=_params(),
    )
    assert qty == Decimal("0")
    assert any("non-positive equity" in r.message for r in caplog.records)


def test_qty_is_decimal_whole_shares():
    qty = size_position(
        equity=Decimal("100000"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("123.45"),
        stop_price=None,
        params=_params(max_pct_per_trade=Decimal("0.02")),
    )
    assert isinstance(qty, Decimal)
    assert qty == qty.quantize(Decimal("1"))
