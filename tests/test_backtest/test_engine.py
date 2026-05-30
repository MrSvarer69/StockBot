from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd


from trading_bot.backtest import BacktestCosts, run_backtest
from trading_bot.contracts import RiskParams
from trading_bot.strategy import ORBStrategy, load_config

from tests.test_strategy.fixtures import make_synthetic_session


def _params() -> RiskParams:
    return RiskParams(
        max_pct_per_trade=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.05"),
    )


def _zero_costs() -> BacktestCosts:
    return BacktestCosts(
        commission_per_share=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def test_no_signals_means_flat_equity():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="none")
    empty = pd.DataFrame(
        columns=[
            "timestamp",
            "symbol",
            "side",
            "target_size_pct",
            "stop_price",
            "take_price",
        ]
    )
    result = run_backtest(
        bars, empty, initial_cash=Decimal("100000"), risk_params=_params()
    )
    assert result.equity_curve.iloc[-1] == 100000.0
    assert result.metrics["num_trades"] == 0


def test_up_breakout_produces_at_least_one_trade():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    result = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        costs=_zero_costs(),
    )
    assert result.metrics["num_trades"] >= 1


def test_no_lookahead_signal_at_t_fills_at_t_plus_1():
    """A signal at bar t must not affect equity at bar t."""
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    entries = signals[signals["side"].isin(["long", "short"])]
    assert not entries.empty
    entry_ts = entries.iloc[0]["timestamp"]

    result = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        costs=_zero_costs(),
    )
    # Equity at the signal bar should still equal cash (no fill yet).
    assert result.equity_curve.loc[entry_ts] == 100000.0


def test_flat_signal_closes_position():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    result = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        costs=_zero_costs(),
    )
    # End of session — position should be closed (one of the trades has exit_reason set).
    assert all(t.exit_reason in {"stop", "take", "time"} for t in result.trades)


def test_metrics_have_expected_keys():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    result = run_backtest(bars, signals, initial_cash=Decimal("100000"))
    for key in (
        "total_return",
        "max_drawdown",
        "sharpe",
        "num_trades",
        "win_rate",
        "avg_win",
        "avg_loss",
        "profit_factor",
        "skipped_signals",
    ):
        assert key in result.metrics


def test_reconcile_passes_with_zero_costs():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    result = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        costs=_zero_costs(),
    )
    assert result.reconcile(Decimal("100000"))


def test_reconcile_passes_with_nonzero_costs():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    result = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        # Non-zero costs — both entry and exit commission charged.
    )
    assert result.reconcile(Decimal("100000"))


def _bars_for_stop_take_test(
    *,
    drop_to: float | None,
    rise_to: float | None,
) -> pd.DataFrame:
    """Build a single-session frame that produces an ORB long entry then drives
    price down to `drop_to` (to test stop) or up to `rise_to` (to test take)."""
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    # After the breakout has fired (which is around 10:00 ET — bar idx 30),
    # overwrite the next 60 bars' close/high/low to drive a known move.
    target = drop_to if drop_to is not None else rise_to
    assert target is not None
    n = len(bars)
    for i in range(35, min(n, 95)):
        bars.iloc[i, bars.columns.get_loc("close")] = target
        bars.iloc[i, bars.columns.get_loc("high")] = target + 0.05
        bars.iloc[i, bars.columns.get_loc("low")] = target - 0.05
        bars.iloc[i, bars.columns.get_loc("open")] = target
    return bars


def test_stop_hit_exits_at_stop():
    bars = _bars_for_stop_take_test(drop_to=80.0, rise_to=None)
    signals = ORBStrategy(load_config()).generate_signals(bars)
    result = run_backtest(
        bars, signals, initial_cash=Decimal("100000"), risk_params=_params(), costs=_zero_costs()
    )
    assert any(t.exit_reason == "stop" for t in result.trades)


def test_take_hit_exits_at_take():
    bars = _bars_for_stop_take_test(drop_to=None, rise_to=150.0)
    signals = ORBStrategy(load_config()).generate_signals(bars)
    result = run_backtest(
        bars, signals, initial_cash=Decimal("100000"), risk_params=_params(), costs=_zero_costs()
    )
    assert any(t.exit_reason == "take" for t in result.trades)


def test_pessimistic_intrabar_picks_stop_when_both_hit():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    # Build a bar after entry where both stop and take are inside [low, high].
    longs = signals[signals["side"] == "long"]
    assert not longs.empty
    entry_ts = longs.iloc[0]["timestamp"]
    entry_idx = bars.index.get_loc(entry_ts)
    sweep_idx = entry_idx + 5  # well after entry, before flat
    stop = float(longs.iloc[0]["stop_price"])
    take = float(longs.iloc[0]["take_price"])
    bars.iloc[sweep_idx, bars.columns.get_loc("low")] = stop - 0.5
    bars.iloc[sweep_idx, bars.columns.get_loc("high")] = take + 0.5
    bars.iloc[sweep_idx, bars.columns.get_loc("open")] = (stop + take) / 2
    bars.iloc[sweep_idx, bars.columns.get_loc("close")] = (stop + take) / 2

    pessimistic = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        costs=BacktestCosts(
            commission_per_share=Decimal("0"),
            slippage_bps=Decimal("0"),
            pessimistic_intrabar=True,
        ),
    )
    optimistic = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        costs=BacktestCosts(
            commission_per_share=Decimal("0"),
            slippage_bps=Decimal("0"),
            pessimistic_intrabar=False,
        ),
    )
    assert pessimistic.trades[0].exit_reason == "stop"
    assert optimistic.trades[0].exit_reason == "take"


def test_nonzero_commissions_reduce_final_equity():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    zero = run_backtest(
        bars, signals, initial_cash=Decimal("100000"), risk_params=_params(), costs=_zero_costs()
    )
    costly = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("100000"),
        risk_params=_params(),
        costs=BacktestCosts(
            commission_per_share=Decimal("1.00"),
            slippage_bps=Decimal("0"),
        ),
    )
    if zero.trades:
        assert costly.equity_curve.iloc[-1] < zero.equity_curve.iloc[-1]


def test_skipped_signals_counted_when_qty_zero():
    bars = make_synthetic_session(date(2026, 1, 5), breakout="up")
    signals = ORBStrategy(load_config()).generate_signals(bars)
    # Force sizing to 0 by giving tiny equity.
    result = run_backtest(
        bars,
        signals,
        initial_cash=Decimal("1"),  # tiny → qty rounds to 0
        risk_params=_params(),
        costs=_zero_costs(),
    )
    assert result.metrics["skipped_signals"] >= 1
    assert result.metrics["num_trades"] == 0
