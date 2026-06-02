"""Alpaca paper-trading broker. `paper=True` is hardcoded — DO NOT parameterize.

A separate AlpacaLiveBroker subclass may be added later, but only after
risk-officer approval and an explicit TRADING_MODE=live gate at the call site.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd

# Alpaca/US-equities tick rules. Limit-style prices (take-profit limit,
# stop trigger, plain limit) must be aligned to these increments or the
# REST API rejects with code 42210000 ("sub-penny increment"). Strategy
# math is pure float arithmetic that routinely produces sub-penny dust
# (e.g. 744.3450000000003 observed 2026-05-22), so quantization here is
# the last line of defense before the wire.
#   - price >= $1.00  →  $0.01 tick (penny)
#   - price <  $1.00  →  $0.0001 tick (sub-penny rule for low-priced)
_TICK_PENNY = Decimal("0.01")
_TICK_SUBPENNY = Decimal("0.0001")
_SUBPENNY_THRESHOLD = Decimal("1.00")

from ..contracts import OrderSide, OrderType, ProposedOrder
from ..data.bars import normalize_alpaca_bars, validate_bars
from .broker import (
    BrokerAccount,
    BrokerAuthError,
    BrokerError,
    BrokerOrderResponse,
    BrokerPosition,
    BrokerValidationError,
)

logger = logging.getLogger(__name__)


def _to_utc(ts: datetime) -> datetime:
    """Normalize any datetime to tz-aware UTC. Project rule: UTC internally."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _parse_side(raw) -> OrderSide:
    """Normalize Alpaca's side representation. Accepts the enum or a string;
    `OrderSide.SELL` stringifies as `OrderSide.SELL`, while a plain string from
    the SDK is `"sell"`/`"buy"`. Match either case-insensitively on the suffix.
    """
    return OrderSide.SELL if str(raw).lower().endswith("sell") else OrderSide.BUY


def _quantize_to_tick(price: Decimal) -> Decimal:
    """Round a Decimal price to the Alpaca-acceptable tick increment.

    Returns the input unchanged for non-positive values — the validation
    layer is responsible for rejecting those; we don't want quantization
    to mask a "stop = 0" bug by silently producing 0.
    """
    if price <= 0:
        return price
    tick = _TICK_PENNY if price >= _SUBPENNY_THRESHOLD else _TICK_SUBPENNY
    return price.quantize(tick, rounding=ROUND_HALF_UP)


def _classify_broker_error(e: Exception, what: str) -> BrokerError:
    """Map an Alpaca SDK exception to a typed BrokerError.

    - HTTP 401 (or any "unauthorized" signal) → `BrokerAuthError` — halt class.
      Misconfigured/revoked API key; no retry will succeed.
    - HTTP 422 → `BrokerValidationError` — halt class. Bot-side bug; retrying
      with the same payload is useless.
    - Everything else (including 403) → generic `BrokerError`. 403 is
      retry-class because Alpaca overloads it for business-rule rejections
      (wash-trade, insufficient qty) that may clear on the next iteration.
    """
    status = getattr(e, "status_code", None)
    msg = f"{what} failed: {e}"
    if status == 401:
        return BrokerAuthError(msg)
    if status == 422:
        return BrokerValidationError(msg)
    return BrokerError(msg)


class AlpacaPaperBroker:
    """Wraps alpaca-py's TradingClient and StockHistoricalDataClient (paper mode)."""

    is_paper: bool = True

    def __init__(self, api_key: str, api_secret: str):
        if not api_key or not api_secret:
            raise BrokerError("ALPACA_API_KEY and ALPACA_API_SECRET are required")
        self._api_key = api_key
        self._api_secret = api_secret
        self._trading = None
        self._data = None

    def _ensure_trading(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient

            self._trading = TradingClient(
                self._api_key, self._api_secret, paper=True
            )
        return self._trading

    def _ensure_data(self):
        if self._data is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._data = StockHistoricalDataClient(self._api_key, self._api_secret)
        return self._data

    def _reset_clients(self) -> None:
        # Drop cached clients so the next _ensure_* call rebuilds with a fresh
        # HTTP session. Recovers from stale keep-alive sockets after laptop
        # sleep, load-balancer connection cull, or any transient drop.
        self._trading = None
        self._data = None

    def is_market_open(self) -> bool:
        try:
            clock = self._ensure_trading().get_clock()
        except Exception as e:
            logger.exception("get_clock failed")
            self._reset_clients()
            raise _classify_broker_error(e, "get_clock") from e
        return bool(clock.is_open)

    def get_account(self) -> BrokerAccount:
        try:
            acct = self._ensure_trading().get_account()
        except Exception as e:
            logger.exception("get_account failed")
            self._reset_clients()
            raise _classify_broker_error(e, "get_account") from e
        return BrokerAccount(
            equity=Decimal(str(acct.equity)),
            cash=Decimal(str(acct.cash)),
            buying_power=Decimal(str(acct.buying_power)),
        )

    def get_positions(self) -> dict[str, BrokerPosition]:
        try:
            positions = self._ensure_trading().get_all_positions()
        except Exception as e:
            logger.exception("get_all_positions failed")
            self._reset_clients()
            raise _classify_broker_error(e, "get_all_positions") from e
        out: dict[str, BrokerPosition] = {}
        for p in positions:
            out[p.symbol] = BrokerPosition(
                symbol=p.symbol,
                qty=Decimal(str(p.qty)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                market_value=Decimal(str(p.market_value)),
            )
        return out

    def get_open_order_count(self) -> int:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            orders = self._ensure_trading().get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
        except Exception as e:
            logger.exception("get_orders failed")
            self._reset_clients()
            raise _classify_broker_error(e, "get_orders") from e
        return len(orders)

    def get_recent_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        try:
            raw = self._ensure_data().get_stock_bars(req).df
        except Exception as e:
            logger.exception("get_stock_bars failed for %s", symbol)
            self._reset_clients()
            raise _classify_broker_error(e, "get_stock_bars") from e

        if raw.empty:
            from ..data.cache import _empty_single_symbol_frame

            return _empty_single_symbol_frame()

        canonical = normalize_alpaca_bars(raw)
        symbols = canonical.index.get_level_values("symbol").unique()
        target = symbol.upper()
        if target in symbols:
            single = canonical.xs(target, level="symbol")
        else:
            single = canonical.xs(symbols[0], level="symbol")
        single.index.name = "timestamp"
        if "symbol" not in single.columns:
            single = single.copy()
            single["symbol"] = target
        validate_bars(single.drop(columns=["symbol"]), single_symbol=True)
        return single

    def get_latest_trade_price(self, symbol: str) -> Decimal:
        from alpaca.data.requests import StockLatestTradeRequest

        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        try:
            resp = self._ensure_data().get_stock_latest_trade(req)
        except Exception as e:
            logger.exception("get_stock_latest_trade failed for %s", symbol)
            self._reset_clients()
            raise _classify_broker_error(e, "get_stock_latest_trade") from e
        trade = resp[symbol]
        return Decimal(str(trade.price))

    def submit_order(self, order: ProposedOrder) -> BrokerOrderResponse:
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        side = AlpacaSide.BUY if order.side == OrderSide.BUY else AlpacaSide.SELL
        # GTC keeps the bracket stop/take alive past the session close so a
        # position held overnight stays protected; DAY (default) expires them
        # at 16:00 ET. Confirmed on live paper 2026-06-01 (probe-overnight check
        # #1): Alpaca accepts GTC on a *market*-parent bracket and carries both
        # stop/take legs as GTC, so no post-fill OCO swap is needed today; if a
        # future broker change rejects market-parent GTC, the fallback is a
        # post-fill GTC OCO swap (see plan Phase 2). submit_order raises (loudly,
        # never silently) on a broker rejection.
        tif = TimeInForce.GTC if order.time_in_force == "gtc" else TimeInForce.DAY
        has_stop = order.stop_price is not None
        has_take = order.take_price is not None
        # Quantize bracket/limit prices to broker tick BEFORE building the
        # request payload. Strategy math is float-based and routinely emits
        # sub-penny dust that Alpaca rejects with code 42210000.
        stop_q = _quantize_to_tick(order.stop_price) if has_stop else None
        take_q = _quantize_to_tick(order.take_price) if has_take else None
        bracket_kind: str | None = None
        if order.order_type == OrderType.MARKET:
            kwargs: dict = dict(
                symbol=order.symbol,
                qty=float(order.qty),
                side=side,
                time_in_force=tif,
                client_order_id=order.client_order_id,
            )
            # Broker-side safety net: attach stop/take as bracket children so the
            # position closes even if the bot crashes or loses connectivity.
            # The poll-side stop/take check in session.py is the primary trigger
            # — brackets are the fallback. cancel_orders_for() in the close
            # path cancels these children when the bot triggers first.
            if has_stop and has_take:
                kwargs["order_class"] = OrderClass.BRACKET
                kwargs["stop_loss"] = StopLossRequest(stop_price=float(stop_q))
                kwargs["take_profit"] = TakeProfitRequest(limit_price=float(take_q))
                bracket_kind = "bracket"
            elif has_stop:
                kwargs["order_class"] = OrderClass.OTO
                kwargs["stop_loss"] = StopLossRequest(stop_price=float(stop_q))
                bracket_kind = "oto_stop"
            elif has_take:
                kwargs["order_class"] = OrderClass.OTO
                kwargs["take_profit"] = TakeProfitRequest(limit_price=float(take_q))
                bracket_kind = "oto_take"
            req = MarketOrderRequest(**kwargs)
        elif order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise BrokerError("limit order missing limit_price")
            limit_q = _quantize_to_tick(order.limit_price)
            req = LimitOrderRequest(
                symbol=order.symbol,
                qty=float(order.qty),
                side=side,
                time_in_force=tif,
                limit_price=float(limit_q),
                client_order_id=order.client_order_id,
            )
        else:
            raise BrokerError(f"unsupported order_type {order.order_type}")

        logger.info(
            "submitting order",
            extra={
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": str(order.qty),
                "type": order.order_type.value,
                "time_in_force": str(tif),
                "client_order_id": order.client_order_id,
                "order_class": bracket_kind,
                # Log the post-quantization values that actually go on the wire.
                # The raw values may differ by up to half a tick (sub-penny dust).
                "stop_price": str(stop_q) if has_stop else None,
                "take_price": str(take_q) if has_take else None,
            },
        )
        try:
            resp = self._ensure_trading().submit_order(req)
        except Exception as e:
            logger.exception("submit_order failed for %s", order.symbol)
            self._reset_clients()
            raise _classify_broker_error(e, "submit_order") from e

        ack = BrokerOrderResponse(
            broker_order_id=str(resp.id),
            client_order_id=getattr(resp, "client_order_id", None),
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            status=str(resp.status),
            submitted_at=_to_utc(resp.submitted_at),
            filled_qty=Decimal(str(getattr(resp, "filled_qty", 0) or 0)),
            filled_avg_price=(
                Decimal(str(resp.filled_avg_price))
                if getattr(resp, "filled_avg_price", None) is not None
                else None
            ),
        )
        logger.info(
            "order accepted",
            extra={
                "broker_order_id": ack.broker_order_id,
                "status": ack.status,
                "symbol": ack.symbol,
            },
        )
        return ack

    def get_order(self, broker_order_id: str) -> BrokerOrderResponse:
        try:
            resp = self._ensure_trading().get_order_by_id(broker_order_id)
        except Exception as e:
            logger.exception("get_order_by_id failed for %s", broker_order_id)
            self._reset_clients()
            raise _classify_broker_error(e, "get_order_by_id") from e
        return BrokerOrderResponse(
            broker_order_id=str(resp.id),
            client_order_id=getattr(resp, "client_order_id", None),
            symbol=resp.symbol,
            side=_parse_side(resp.side),
            qty=Decimal(str(resp.qty)),
            status=str(resp.status),
            submitted_at=_to_utc(resp.submitted_at),
            filled_qty=Decimal(str(getattr(resp, "filled_qty", 0) or 0)),
            filled_avg_price=(
                Decimal(str(resp.filled_avg_price))
                if getattr(resp, "filled_avg_price", None) is not None
                else None
            ),
        )

    def close_position(self, symbol: str) -> BrokerOrderResponse | None:
        try:
            resp = self._ensure_trading().close_position(symbol)
        except Exception as e:
            msg = str(e).lower()
            if "position does not exist" in msg or "not found" in msg:
                logger.info("close_position no-op: %s has no position", symbol)
                return None
            logger.exception("close_position failed for %s", symbol)
            self._reset_clients()
            raise _classify_broker_error(e, "close_position") from e
        return BrokerOrderResponse(
            broker_order_id=str(resp.id),
            client_order_id=getattr(resp, "client_order_id", None),
            symbol=symbol,
            side=_parse_side(resp.side),
            qty=Decimal(str(resp.qty)),
            status=str(resp.status),
            submitted_at=_to_utc(resp.submitted_at),
            filled_qty=Decimal(str(getattr(resp, "filled_qty", 0) or 0)),
            filled_avg_price=(
                Decimal(str(resp.filled_avg_price))
                if getattr(resp, "filled_avg_price", None) is not None
                else None
            ),
        )

    def cancel_orders_for(self, symbol: str) -> int:
        """Cancel any open orders for `symbol`. Returns the number canceled.

        Idempotent: a missing or already-terminal order is treated as success.
        Called before `close_position` to avoid the wash-trade rejection that
        Alpaca returns when an opposite-side market order is in flight.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            orders = self._ensure_trading().get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[symbol],
                )
            )
        except Exception as e:
            logger.exception("get_orders(symbol=%s) failed", symbol)
            self._reset_clients()
            raise _classify_broker_error(e, f"get_orders for {symbol}") from e

        canceled = 0
        for o in orders:
            try:
                self._ensure_trading().cancel_order_by_id(o.id)
                canceled += 1
            except Exception:
                # Order already terminal or canceled by another path — log and
                # continue. We do not raise: callers (e.g. _handle_flat_signal)
                # treat this as best-effort cleanup.
                logger.exception(
                    "cancel_order_by_id failed for %s (symbol=%s)", o.id, symbol
                )
        return canceled

    def cancel_all_orders(self) -> None:
        try:
            self._ensure_trading().cancel_orders()
        except Exception as e:
            logger.exception("cancel_orders failed")
            self._reset_clients()
            raise _classify_broker_error(e, "cancel_orders") from e

    def replace_stop_price(
        self, symbol: str, new_stop_price: Decimal
    ) -> bool:
        """Update the broker-side stop leg's stop_price for `symbol`.

        Uses Alpaca's native replace endpoint so the position is never
        briefly unprotected by a cancel+place race. Returns True on
        successful replacement or no-op (already at the requested price);
        returns False if there is no open stop leg to replace (position
        already closed, bracket already fired) OR if more than one genuinely
        live stop leg exists for the symbol (ambiguous — see below). Raises
        BrokerError on fatal failures.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest, ReplaceOrderRequest

        quantized = _quantize_to_tick(new_stop_price)

        try:
            # Query ALL (symbol-filtered, newest-first) with nested=True, NOT
            # OPEN. A bracket's protective stop child sits in `HELD` status
            # nested under the *filled* parent: once the market parent fills,
            # Alpaca keeps the OCO pair as children of the now-closed parent and
            # holds the stop leg until the take-profit is touched.
            # QueryOrderStatus.OPEN excludes BOTH the closed parent AND its held
            # leg, so an OPEN query surfaces only the live take-profit limit leg
            # — the adapter then logged "no open stop leg found" every poll and
            # trailing never ratcheted (observed 2026-05-27 and again 2026-06-02
            # on XLE: parent FILLED, stop child HELD@56.41, invisible to OPEN).
            # ALL makes the filled parent and its held stop child visible; the
            # terminal-status filter below drops prior completed brackets.
            # Bound the page with an explicit `limit` (newest-first) so the
            # live bracket can never silently fall off the default page and
            # revert this method to "no stop found" → frozen trail. We do NOT
            # add an `after` time window: this broker backs overnight holds, so
            # the protective bracket's parent may have filled on a *prior*
            # session — a same-day window would re-hide exactly the overnight
            # stop this path exists to protect. The query is symbol-scoped, so
            # per-symbol order volume stays small and 100 is comfortably ample.
            orders = self._ensure_trading().get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    symbols=[symbol],
                    nested=True,
                    limit=100,
                )
            )
        except Exception as e:
            logger.exception(
                "get_orders failed in replace_stop_price for %s", symbol
            )
            self._reset_clients()
            raise _classify_broker_error(
                e, f"get_orders for {symbol}"
            ) from e

        # Flatten top-level orders together with any nested bracket children.
        # `getattr(o, "legs", None)` is only traversed when it is a real
        # list/tuple — an SDK order with no children exposes `legs=None`, and
        # test doubles leave it unset; either way we must not try to iterate a
        # non-sequence.
        candidates = []
        for o in orders:
            candidates.append(o)
            legs = getattr(o, "legs", None)
            if isinstance(legs, (list, tuple)):
                candidates.extend(legs)

        # Substring-match the order_type, not exact membership: the live SDK
        # hands back an `OrderType` enum whose `str()` is "OrderType.STOP"
        # (lowercased "ordertype.stop"), so an exact `in {"stop", ...}` test
        # never fires on real broker data — the same enum-repr trap the probe
        # hit with OrderType.LIMIT. Match any type containing "stop" while
        # explicitly excluding "trailing_stop" (a distinct Alpaca family with
        # broker-managed dynamic stops): the bot's trailing logic is
        # intentionally bot-side, so a broker-side trailing leg must not be
        # silently overwritten here. This covers "stop", "stop_loss",
        # "stop_limit" and their "OrderType.*" enum reprs.
        #
        # Exclude terminal legs by status: status=ALL also returns prior
        # completed brackets whose stop child is CANCELED/FILLED/etc. Selecting
        # one would replace a dead order or, with two, trip the multi-leg
        # warning against history. Status is matched as a substring for the
        # same enum-repr reason ("OrderStatus.CANCELED"); a leg with no/unknown
        # status is treated as live, so the simpler unit-test mocks still match.
        # "stopped" = the stop already triggered/executed → terminal, must not
        # be re-replaced. ("partially_filled" contains "filled" and is thus
        # treated terminal too: a half-filled stop means the position is partly
        # gone and should not keep ratcheting — acceptable and intentional.)
        _TERMINAL_STATUSES = (
            "filled", "canceled", "expired",
            "rejected", "replaced", "done_for_day", "stopped",
        )

        def _is_live_stop(o) -> bool:
            type_str = str(getattr(o, "order_type", "")).lower()
            if "stop" not in type_str or "trailing" in type_str:
                return False
            status_str = str(getattr(o, "status", "")).lower()
            return not any(t in status_str for t in _TERMINAL_STATUSES)

        stop_legs = [o for o in candidates if _is_live_stop(o)]
        if not stop_legs:
            logger.info(
                "replace_stop_price: no open stop leg found",
                extra={"symbol": symbol, "new_stop_price": str(quantized)},
            )
            return False
        if len(stop_legs) > 1:
            # Two+ genuinely live stop legs means more than one open
            # bracket/position on this symbol. We cannot tell which one the
            # caller's `new_stop_price` was computed for, and ratcheting the
            # wrong bracket's stop could stop it out prematurely or loosen the
            # other's protection. Refuse and warn rather than guess `[0]`: the
            # existing stops stay in place (the position remains protected at
            # current levels) and the caller treats False as "skip this poll".
            logger.warning(
                "replace_stop_price: multiple live stop legs; refusing to guess",
                extra={"symbol": symbol, "count": len(stop_legs)},
            )
            return False
        leg = stop_legs[0]

        existing = getattr(leg, "stop_price", None)
        if existing is not None and Decimal(str(existing)) == quantized:
            return True

        try:
            self._ensure_trading().replace_order_by_id(
                order_id=leg.id,
                order_data=ReplaceOrderRequest(stop_price=float(quantized)),
            )
        except Exception as e:
            logger.exception(
                "replace_order_by_id failed for stop leg %s (symbol=%s)",
                leg.id,
                symbol,
            )
            self._reset_clients()
            raise _classify_broker_error(
                e, f"replace_stop_price for {symbol}"
            ) from e

        logger.info(
            "replaced stop leg",
            extra={
                "symbol": symbol,
                "broker_order_id": str(leg.id),
                "old_stop_price": str(existing) if existing is not None else None,
                "new_stop_price": str(quantized),
            },
        )
        return True
