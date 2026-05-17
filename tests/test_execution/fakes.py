"""In-memory broker fake. Implements the BrokerClient Protocol for tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pandas as pd

from trading_bot.contracts import OrderSide, ProposedOrder
from trading_bot.execution.broker import (
    BrokerAccount,
    BrokerOrderResponse,
    BrokerPosition,
)


@dataclass
class FakeBroker:
    bars_by_symbol: dict[str, pd.DataFrame] = field(default_factory=dict)
    latest_prices: dict[str, Decimal] = field(default_factory=dict)
    equity: Decimal = Decimal("100000")
    cash: Decimal = Decimal("100000")
    market_open: bool = True
    is_paper: bool = True

    positions: dict[str, BrokerPosition] = field(default_factory=dict)
    submitted_orders: list[ProposedOrder] = field(default_factory=list)
    closed_symbols: list[str] = field(default_factory=list)
    cancel_calls: int = 0
    fail_on_submit: bool = False
    order_responses: dict[str, BrokerOrderResponse] = field(default_factory=dict)

    def is_market_open(self) -> bool:
        return self.market_open

    def get_account(self) -> BrokerAccount:
        return BrokerAccount(
            equity=self.equity,
            cash=self.cash,
            buying_power=self.cash * Decimal("2"),
        )

    def get_positions(self) -> dict[str, BrokerPosition]:
        return dict(self.positions)

    def get_open_order_count(self) -> int:
        return 0

    def get_recent_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        df = self.bars_by_symbol.get(symbol, pd.DataFrame())
        if df.empty:
            return df.copy()
        return df[(df.index >= start) & (df.index <= end)].copy()

    def get_latest_trade_price(self, symbol: str) -> Decimal:
        return self.latest_prices.get(symbol, Decimal("100"))

    def submit_order(self, order: ProposedOrder) -> BrokerOrderResponse:
        from trading_bot.execution.broker import BrokerError

        if self.fail_on_submit:
            raise BrokerError("simulated failure")

        self.submitted_orders.append(order)
        signed_qty = order.qty if order.side == OrderSide.BUY else -order.qty
        prev = self.positions.get(order.symbol)
        new_qty = (prev.qty if prev else Decimal("0")) + signed_qty
        if new_qty == 0:
            self.positions.pop(order.symbol, None)
        else:
            self.positions[order.symbol] = BrokerPosition(
                symbol=order.symbol,
                qty=new_qty,
                avg_entry_price=self.latest_prices.get(order.symbol, Decimal("100")),
                market_value=new_qty * self.latest_prices.get(order.symbol, Decimal("100")),
            )
        resp = BrokerOrderResponse(
            broker_order_id=str(uuid4()),
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            status="accepted",
            submitted_at=datetime.now(UTC),
            filled_qty=order.qty,
            filled_avg_price=self.latest_prices.get(order.symbol, Decimal("100")),
        )
        self.order_responses[resp.broker_order_id] = resp
        return resp

    def get_order(self, broker_order_id: str) -> BrokerOrderResponse:
        from trading_bot.execution.broker import BrokerError

        if broker_order_id not in self.order_responses:
            raise BrokerError(f"unknown order {broker_order_id}")
        return self.order_responses[broker_order_id]

    def close_position(self, symbol: str) -> BrokerOrderResponse | None:
        if symbol not in self.positions:
            return None
        pos = self.positions.pop(symbol)
        self.closed_symbols.append(symbol)
        resp = BrokerOrderResponse(
            broker_order_id=str(uuid4()),
            client_order_id=None,
            symbol=symbol,
            side=OrderSide.SELL if pos.qty > 0 else OrderSide.BUY,
            qty=abs(pos.qty),
            status="filled",
            submitted_at=datetime.now(UTC),
            filled_qty=abs(pos.qty),
            filled_avg_price=self.latest_prices.get(symbol, Decimal("100")),
        )
        self.order_responses[resp.broker_order_id] = resp
        return resp

    def cancel_orders_for(self, symbol: str) -> int:
        # FakeBroker fills synchronously, so there are no pending orders to
        # cancel. Tests that need to simulate a pending-order rejection
        # subclass this method explicitly.
        return 0

    def cancel_all_orders(self) -> None:
        self.cancel_calls += 1
