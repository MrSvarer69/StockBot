"""Probe whether Alpaca paper accepts a GTC market-parent bracket order.

This is live-paper verification #1 for the overnight-holding feature (plan
`snappy-orbiting-hamster`): the whole Phase-2 design assumes Alpaca accepts
`time_in_force=GTC` on a MARKET bracket so the stop/take legs survive the close.
This script settles that question in isolation, during market hours, with one
share — instead of running the whole bot.

What it does:
  1. Builds a 1-share MARKET bracket (stop 10% below, take 10% above the last
     trade, so neither leg triggers immediately) with time_in_force="gtc".
  2. Routes it through `trading_bot.risk.validate_order` (no direct broker call
     that bypasses risk — CLAUDE.md rule 4).
  3. Submits it. A BrokerValidationError here means Alpaca REJECTED the GTC
     market bracket → check #1 FAILS → the OCO-swap fallback is required.
  4. On success, reads the open orders back and prints each leg's type, status,
     and time_in_force so you can confirm the stop/take children are GTC.
  5. Cleans up (cancels orders + closes the position) unless --keep.

Usage (needs ALPACA_API_KEY/SECRET in .env and TRADING_MODE=paper):
    uv run python scripts/probe_overnight_protection.py            # check #1, self-cleans
    uv run python scripts/probe_overnight_protection.py --keep     # leave it open to check #2 tomorrow
    uv run python scripts/probe_overnight_protection.py --cleanup-only --symbol SPY
"""

from __future__ import annotations

import argparse
import os
from decimal import Decimal

from dotenv import load_dotenv

from trading_bot.contracts import (
    AccountState,
    OrderSide,
    OrderType,
    Position,
    ProposedOrder,
    RiskParams,
)
from trading_bot.execution import AlpacaPaperBroker
from trading_bot.execution.broker import BrokerValidationError
from trading_bot.risk import validate_order


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="SPY", help="Liquid symbol to probe (default SPY)")
    p.add_argument("--qty", type=int, default=1, help="Share quantity (default 1)")
    p.add_argument(
        "--keep",
        action="store_true",
        help="Do NOT clean up — leave the position + GTC legs open so you can "
        "confirm overnight survival (check #2) the next morning.",
    )
    p.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Skip the probe; just cancel open orders and close the position "
        "for --symbol (teardown after a --keep run).",
    )
    return p.parse_args(argv)


def _broker():
    load_dotenv()
    mode = os.environ.get("TRADING_MODE", "paper")
    if mode != "paper":
        raise SystemExit(f"ERROR: TRADING_MODE={mode!r}; this probe is paper-only.")
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not sec:
        raise SystemExit("ERROR: ALPACA_API_KEY / ALPACA_API_SECRET not set in .env")
    return AlpacaPaperBroker(api_key=key, api_secret=sec)


def _print_open_legs(broker: AlpacaPaperBroker, symbol: str) -> None:
    """Print every open order (and nested bracket leg) for the symbol."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = broker._ensure_trading().get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=True)
    )
    rows = []
    for o in orders:
        rows.append(o)
        legs = getattr(o, "legs", None)
        if isinstance(legs, (list, tuple)):
            rows.extend(legs)
    print(f"  open orders for {symbol}: {len(rows)}")
    for o in rows:
        print(
            f"    type={str(getattr(o, 'order_type', '?')):<12} "
            f"status={str(getattr(o, 'status', '?')):<18} "
            f"tif={str(getattr(o, 'time_in_force', '?')):<10} "
            f"stop={getattr(o, 'stop_price', None)} limit={getattr(o, 'limit_price', None)}"
        )
    gtc_legs = [
        o for o in rows
        if "gtc" in str(getattr(o, "time_in_force", "")).lower()
        and str(getattr(o, "order_type", "")).lower() in {"stop", "stop_loss", "stop_limit", "limit"}
    ]
    if gtc_legs:
        print(f"  ✓ {len(gtc_legs)} GTC protective leg(s) present — check #1 PASSES.")
    else:
        print("  ✗ no GTC protective legs found — inspect output above.")


def _cleanup(broker: AlpacaPaperBroker, symbol: str) -> None:
    print(f"cleaning up {symbol} ...")
    canceled = broker.cancel_orders_for(symbol)
    print(f"  canceled {canceled} open order(s)")
    resp = broker.close_position(symbol)
    print(f"  close_position: {'no position' if resp is None else resp.status}")


def main(argv=None) -> int:
    args = _parse_args(argv)
    broker = _broker()
    symbol = args.symbol.upper()

    if args.cleanup_only:
        _cleanup(broker, symbol)
        return 0

    if not broker.is_market_open():
        print(
            "WARNING: market is closed — a MARKET order will not fill now, so "
            "this won't fully exercise the bracket. Re-run during RTH for a "
            "definitive check #1 result."
        )

    price = broker.get_latest_trade_price(symbol)
    stop = price * Decimal("0.90")
    take = price * Decimal("1.10")
    qty = Decimal(str(args.qty))
    print(f"probing {symbol}: last={price}  qty={qty}  stop={stop:.2f}  take={take:.2f}  tif=gtc")

    order = ProposedOrder(
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qty,
        order_type=OrderType.MARKET,
        stop_price=stop,
        take_price=take,
        client_order_id=f"probe-overnight-{symbol}",
        time_in_force="gtc",
    )

    # Route through the risk layer (rule 4). Permissive params + a real account
    # snapshot so the single probe order is sanity-checked, not blindly placed.
    acct = broker.get_account()
    state = AccountState(
        equity=acct.equity,
        cash=acct.cash,
        open_positions={
            s: Position(symbol=s, qty=p.qty, avg_entry_price=p.avg_entry_price)
            for s, p in broker.get_positions().items()
        },
    )
    params = RiskParams(
        max_pct_per_trade=Decimal("1"),
        max_daily_notional_pct=Decimal("1"),
        symbol_whitelist=frozenset({symbol}),
        kill_file_path="/nonexistent/probe/KILL",
        kill_env_var="PROBE_NO_KILL",
    )
    decision = validate_order(order, state, params, market_is_open=True, current_price=price)
    if not decision.approved or decision.order is None:
        print(f"risk rejected the probe order: {decision.reason}")
        return 1

    try:
        ack = broker.submit_order(decision.order)
    except BrokerValidationError as exc:
        print(
            "\n✗ CHECK #1 FAILED: Alpaca REJECTED the GTC market bracket "
            f"(BrokerValidationError): {exc}\n"
            "  → market+GTC bracket is not supported. Implement the OCO-swap "
            "fallback (plan Phase 2) before enabling protect_overnight."
        )
        return 2
    print(f"submit accepted: broker_order_id={ack.broker_order_id} status={ack.status}")

    _print_open_legs(broker, symbol)

    if args.keep:
        print(
            "\n--keep set: leaving the position + GTC legs OPEN. Tomorrow, before "
            "the next open, re-run with --cleanup-only (or check the Alpaca "
            "dashboard) to confirm the legs survived overnight (check #2) and "
            "that avg_entry_price is unchanged (check #3)."
        )
    else:
        print()
        _cleanup(broker, symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
