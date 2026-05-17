"""Broker abstraction. The Protocol seam keeps IBKR/other implementations open.

NO strategy code may import a concrete broker. Only the session loop talks to
brokers, and every order goes through `trading_bot.risk.validate_order` first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

import pandas as pd

from ..contracts import OrderSide, ProposedOrder


class BrokerError(RuntimeError):
    """Any broker-side failure. Always raised, never swallowed."""


class BrokerAuthError(BrokerError):
    """Authentication-class failure (HTTP 401). Indicates a misconfigured or
    revoked API key — there is no retry that can succeed without operator
    intervention, so the session loop halts on this immediately rather than
    burning `consecutive_failures` budget."""


class BrokerValidationError(BrokerError):
    """Client-side validation failure (HTTP 422). Indicates malformed input or
    a precondition the broker rejected. Halts on the same logic as auth: the
    bug is in the bot or its config; retrying will not help."""


@dataclass(frozen=True)
class BrokerAccount:
    """A snapshot of the broker's view of the account."""

    equity: Decimal
    cash: Decimal
    buying_power: Decimal


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: Decimal  # signed: positive = long, negative = short
    avg_entry_price: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class BrokerOrderResponse:
    """Acknowledgement of an order submission. Fills come asynchronously."""

    broker_order_id: str
    client_order_id: str | None
    symbol: str
    side: OrderSide
    qty: Decimal
    status: str  # "accepted" | "filled" | "rejected" | etc.
    submitted_at: datetime
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Decimal | None = None


class BrokerClient(Protocol):
    """Minimal broker surface needed by the session loop."""

    @property
    def is_paper(self) -> bool: ...

    def is_market_open(self) -> bool: ...

    def get_account(self) -> BrokerAccount: ...

    def get_positions(self) -> dict[str, BrokerPosition]: ...

    def get_open_order_count(self) -> int: ...

    def get_recent_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame: ...

    def get_latest_trade_price(self, symbol: str) -> Decimal: ...

    def submit_order(self, order: ProposedOrder) -> BrokerOrderResponse: ...

    def get_order(self, broker_order_id: str) -> BrokerOrderResponse: ...

    def close_position(self, symbol: str) -> BrokerOrderResponse | None: ...

    def cancel_orders_for(self, symbol: str) -> int: ...

    def cancel_all_orders(self) -> None: ...
