from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.risk import RiskParams, effective_equity, size_position


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


# ---------------------------------------------------------------------------
# max_capital_usd behavior — sizing must use effective_equity, not raw equity.
# ---------------------------------------------------------------------------


def test_max_capital_cap_binds_when_below_equity():
    # equity 10k, cap 500 — sizing must act as if bankroll were 500.
    # target_notional = 500 * 0.10 = 50
    # cap_notional    = 500 * 0.02 = 10  (binds)
    # entry=$1, no stop -> qty = 10/1 = 10 shares.
    # If sizing wrongly used the raw $10k equity, per-trade cap would be
    # 10000 * 0.02 = 200 -> 200 shares. We pin the cap-honouring path.
    qty = size_position(
        equity=Decimal("10000"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("1"),
        stop_price=None,
        params=_params(
            max_pct_per_trade=Decimal("0.02"),
            max_capital_usd=Decimal("500"),
        ),
    )
    assert qty == Decimal("10")


def test_max_capital_cap_loose_uses_raw_equity():
    # equity 100, cap 500 -> cap is loose; effective_equity = 100.
    # target_notional = 100 * 0.10 = 10
    # cap_notional    = 100 * 0.02 = 2  (binds)
    # entry=$1 -> qty = 2.
    qty = size_position(
        equity=Decimal("100"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("1"),
        stop_price=None,
        params=_params(
            max_pct_per_trade=Decimal("0.02"),
            max_capital_usd=Decimal("500"),
        ),
    )
    assert qty == Decimal("2")
    # Sanity: same inputs with cap=None must produce the same result —
    # the cap is genuinely irrelevant here.
    qty_no_cap = size_position(
        equity=Decimal("100"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("1"),
        stop_price=None,
        params=_params(
            max_pct_per_trade=Decimal("0.02"),
            max_capital_usd=None,
        ),
    )
    assert qty_no_cap == qty


def test_max_capital_zero_returns_zero_with_warning(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="trading_bot.risk.sizing")
    qty = size_position(
        equity=Decimal("10000"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("100"),
        stop_price=None,
        params=_params(
            max_pct_per_trade=Decimal("0.02"),
            max_capital_usd=Decimal("0"),
        ),
    )
    assert qty == Decimal("0")
    assert any(
        "non-positive capped equity" in r.message for r in caplog.records
    )


def test_max_capital_none_matches_pre_cap_baseline():
    # Baseline: equity 10k, target 10%, per-trade 2%, no stop, no cap.
    # cap_notional = 10000 * 0.02 = 200 (binds), entry=$100 -> qty = 2.
    qty = size_position(
        equity=Decimal("10000"),
        target_pct=Decimal("0.10"),
        entry_price=Decimal("100"),
        stop_price=None,
        params=_params(
            max_pct_per_trade=Decimal("0.02"),
            max_capital_usd=None,
        ),
    )
    assert qty == Decimal("2")


@pytest.mark.parametrize(
    "equity, cap, expected",
    [
        (Decimal("10000"), Decimal("500"), Decimal("500")),  # cap binds
        (Decimal("100"), Decimal("500"), Decimal("100")),  # equity binds
        (Decimal("500"), Decimal("500"), Decimal("500")),  # tie -> cap value
        (Decimal("10000"), None, Decimal("10000")),  # no cap -> raw
        (Decimal("0"), Decimal("500"), Decimal("0")),  # zero equity
        (Decimal("10000"), Decimal("0"), Decimal("0")),  # zero cap
    ],
)
def test_effective_equity_returns_min_of_equity_and_cap(equity, cap, expected):
    params = _params(max_capital_usd=cap)
    assert effective_equity(equity, params) == expected
