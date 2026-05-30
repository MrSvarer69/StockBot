"""Shared types and shape conventions used across data, strategy, risk, and backtest.

These are deliberately small dataclasses + docstrings describing DataFrame shapes.
They form the contract every module in `trading_bot/` agrees on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Literal


# ---------------------------------------------------------------------------
# DataFrame shape conventions (documented, not types because pandas)
# ---------------------------------------------------------------------------

# Bars DataFrame ("BarsFrame")
#   Single-symbol: DatetimeIndex named "timestamp", tz=UTC.
#   Multi-symbol : MultiIndex (symbol: str, timestamp: tz=UTC).
#   Columns: open, high, low, close (float64), volume (int64).
#   Optional   : trade_count (int64), vwap (float64).
#
# Signals DataFrame ("SignalsFrame")
#   Columns: timestamp (tz=UTC), symbol (str), side ("long"|"short"|"flat"),
#            target_size_pct (float in [0, 1]),
#            stop_price (float | nan), take_price (float | nan).
#   One row per signal event, not per bar.


Side = Literal["long", "short", "flat"]


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


# ---------------------------------------------------------------------------
# Money-domain dataclasses — Decimal for anything that is currency or shares.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskParams:
    """Static risk configuration. All percentages expressed as fractions (0.01 == 1%)."""

    max_pct_per_trade: Decimal = Decimal("0.02")
    max_daily_loss_pct: Decimal = Decimal("0.03")
    max_daily_notional_pct: Decimal = Decimal("0.50")
    max_position_count: int = 5
    max_open_orders: int = 10
    symbol_whitelist: frozenset[str] = field(default_factory=frozenset)
    kill_file_path: str = "data/ops/KILL"
    kill_env_var: str = "TRADING_KILL"
    # Hard cap on the equity used for sizing and percentage-based risk caps.
    # When set, every "% of equity" calculation in the risk module operates
    # on min(account_equity, max_capital_usd) instead of raw broker equity.
    # Use case: a paper account holds $100k but the operator only intends to
    # deploy USD 500 (≈3000 DKK at ~6.9 DKK/USD) in real life — set this so
    # backtest and paper sizing reflect that real-world ceiling. None = no
    # cap (raw equity used; current default).
    max_capital_usd: Decimal | None = None


@dataclass
class Position:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal


@dataclass
class AccountState:
    equity: Decimal
    cash: Decimal
    realized_pnl_today: Decimal = Decimal("0")
    cumulative_notional_today: Decimal = Decimal("0")
    open_positions: dict[str, Position] = field(default_factory=dict)
    open_order_count: int = 0
    connection_ok: bool = True


@dataclass
class ProposedOrder:
    symbol: str
    side: OrderSide
    qty: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_price: Decimal | None = None
    client_order_id: str | None = None
    # Broker time-in-force: "day" (default; bracket children expire at the
    # close) or "gtc" (good-till-canceled; bracket stop/take survive overnight
    # so a held position stays protected). Set "gtc" only when the position is
    # intended to be carried past the session close.
    time_in_force: str = "day"


@dataclass
class OrderDecision:
    approved: bool
    reason: str
    order: ProposedOrder | None = None


# ---------------------------------------------------------------------------
# Backtest results
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    """A single round-trip trade — entry + exit pair."""

    symbol: str
    side: Side
    entry_time: object  # pd.Timestamp; kept loose so contracts.py has no pandas dep
    exit_time: object
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    pnl: Decimal
    exit_reason: str  # "stop" | "take" | "time" | "session_end"
    strategy: str = ""  # Name of the bot that produced the entry signal: "orb" | "pullback" | "insider". Default empty for old backtest fixtures.


@dataclass
class BacktestResult:
    equity_curve: object  # pd.Series, indexed by UTC timestamp
    trades: list[Trade]
    metrics: dict[str, float]
    config: dict[str, object]

    def reconcile(self, initial_cash: Decimal, tol: Decimal = Decimal("0.000001")) -> bool:
        """Verify initial_cash + sum(trade.pnl) == final_equity within tolerance.

        Returns True on success, raises AssertionError on mismatch. Useful as a
        smoke check that the engine's cash/pnl bookkeeping stays symmetric.
        """
        import pandas as pd  # local: contracts.py must stay pandas-free at import

        if self.equity_curve is None or len(self.equity_curve) == 0:
            return True
        final_equity = Decimal(str(pd.Series(self.equity_curve).iloc[-1]))
        expected = initial_cash + sum((t.pnl for t in self.trades), Decimal("0"))
        if abs(final_equity - expected) > tol:
            raise AssertionError(
                f"reconcile failed: final={final_equity} expected={expected}"
            )
        return True
