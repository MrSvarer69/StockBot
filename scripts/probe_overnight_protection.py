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

Separately, `--check-replace` verifies the trailing-stop adapter: that
`replace_stop_price` can safely update a bracket's stop child once it is HELD
under a filled parent (the case that broke trailing on 2026-06-02). It submits a
DAY market bracket, waits for the fill, replaces the HELD stop, and confirms the
position stays continuously protected by exactly one live stop at the new price.

Usage (needs ALPACA_API_KEY/SECRET in .env and TRADING_MODE=paper):
    uv run python scripts/probe_overnight_protection.py            # check #1, self-cleans
    uv run python scripts/probe_overnight_protection.py --keep     # leave it open to check overnight survival tomorrow
    uv run python scripts/probe_overnight_protection.py --check-replace   # HELD-stop replace safety, self-cleans
    uv run python scripts/probe_overnight_protection.py --cleanup-only --symbol SPY
"""

from __future__ import annotations

import argparse
import os
import uuid
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
    p.add_argument(
        "--check-replace",
        action="store_true",
        help="Check #2 (HELD-stop replace safety): submit a DAY market bracket, "
        "wait for the parent to FILL so the stop child goes HELD, then call "
        "replace_stop_price and verify the position stays continuously "
        "protected by exactly one live stop at the new price. Always cleans up.",
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
    # str(OrderType.LIMIT) is "OrderType.LIMIT", so an exact-match membership
    # test against {"limit", ...} never fires — substring-match the enum repr,
    # the same way the tif check above does.
    gtc_legs = [
        o for o in rows
        if "gtc" in str(getattr(o, "time_in_force", "")).lower()
        and any(t in str(getattr(o, "order_type", "")).lower() for t in ("stop", "limit"))
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


# Mirror of the production terminal-status filter in replace_stop_price, so this
# probe classifies legs exactly the way the adapter under test does.
_PROBE_TERMINAL_STATUSES = (
    "filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "stopped",
)


def _all_orders_nested(broker: AlpacaPaperBroker, symbol: str) -> list:
    """Every order for `symbol` from the ALL-status nested query, flattened with
    its bracket children — the same shape replace_stop_price now scans."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = broker._ensure_trading().get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, symbols=[symbol], nested=True, limit=100
        )
    )
    flat = []
    for o in orders:
        flat.append(o)
        legs = getattr(o, "legs", None)
        if isinstance(legs, (list, tuple)):
            flat.extend(legs)
    return flat


def _live_stop_legs(flat: list) -> list:
    """Non-terminal stop-family legs (excluding broker-side trailing_stop)."""
    out = []
    for o in flat:
        t = str(getattr(o, "order_type", "")).lower()
        if "stop" not in t or "trailing" in t:
            continue
        s = str(getattr(o, "status", "")).lower()
        if any(term in s for term in _PROBE_TERMINAL_STATUSES):
            continue
        out.append(o)
    return out


def _check_replace(broker: AlpacaPaperBroker, symbol: str, qty: int) -> int:
    """Check #2: prove replace_stop_price safely updates a HELD stop child.

    Reproduces the live failure shape (DAY market bracket → parent FILLS → stop
    child HELD), then replaces the stop and verifies the position is never left
    without a live protective stop and ends with exactly one at the new price.
    """
    import time

    from trading_bot.execution.alpaca_paper import _quantize_to_tick
    from trading_bot.execution.broker import BrokerError

    if not broker.is_market_open():
        print(
            "⚠ INCONCLUSIVE: market is closed — a MARKET parent will not fill, so "
            "no HELD stop child is produced. Re-run during RTH."
        )
        return 3

    price = broker.get_latest_trade_price(symbol)
    stop = price * Decimal("0.90")   # 10% below: won't trigger
    take = price * Decimal("1.10")   # 10% above: won't trigger
    qd = Decimal(str(qty))
    print(
        f"check #2 (HELD-stop replace): {symbol} last={price} qty={qd} "
        f"stop={stop:.2f} take={take:.2f} tif=day"
    )

    order = ProposedOrder(
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qd,
        order_type=OrderType.MARKET,
        stop_price=stop,
        take_price=take,
        client_order_id=f"probe-replace-{symbol}-{uuid.uuid4().hex[:8]}",
        # tif defaults to DAY — the faithful reproduction of the live XLE bracket
        # whose stop child went HELD. The HELD-replace question is tif-independent.
    )

    # Route through the risk layer (rule 4), same as check #1.
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

    ack = broker.submit_order(decision.order)
    parent_id = str(ack.broker_order_id)
    print(f"submitted parent {parent_id[:8]} status={ack.status}; waiting for fill + HELD stop ...")

    deadline = time.time() + 40
    stops: list = []
    parent_filled = False
    while time.time() < deadline:
        flat = _all_orders_nested(broker, symbol)
        parent_filled = any(
            str(getattr(o, "id", "")) == parent_id
            and "filled" in str(getattr(o, "status", "")).lower()
            for o in flat
        )
        stops = _live_stop_legs(flat)
        if parent_filled and stops:
            break
        time.sleep(2)

    if not (parent_filled and stops):
        print(
            f"⚠ INCONCLUSIVE: parent_filled={parent_filled}, live_stops={len(stops)} "
            "after 40s. Could not reproduce HELD stop. Cleaning up."
        )
        _cleanup(broker, symbol)
        return 3

    orig = stops[0]
    print(
        f"  pre-replace: {len(stops)} live stop leg(s); "
        f"id={str(orig.id)[:8]} status={orig.status} stop={orig.stop_price}"
    )

    new_stop = price * Decimal("0.92")  # tighter but still well below last → protective, won't trigger
    quant = _quantize_to_tick(new_stop)
    print(f"  calling replace_stop_price({symbol}, {new_stop:.2f}) → quantized {quant} ...")
    try:
        result = broker.replace_stop_price(symbol, new_stop)
    except BrokerError as e:
        print(
            f"\n✗ CHECK #2 FAILED: replace_stop_price RAISED on a HELD leg: {e}\n"
            "  → Alpaca rejects replacing a held stop. The adapter degrades safe "
            "(old stop kept, error caught upstream), but the trail cannot ratchet "
            "via replace — an OCO cancel+reissue fallback is required."
        )
        _cleanup(broker, symbol)
        return 2

    flat2 = _all_orders_nested(broker, symbol)
    stops2 = _live_stop_legs(flat2)
    orig_after = next((o for o in flat2 if str(getattr(o, "id", "")) == str(orig.id)), None)
    orig_disp = str(getattr(orig_after, "status", "<absent>")) if orig_after else "<absent>"
    print(f"  replace_stop_price returned {result}")
    print(f"  post-replace: {len(stops2)} live stop leg(s); original leg disposition={orig_disp}")
    for o in stops2:
        print(f"    live stop id={str(o.id)[:8]} status={o.status} stop={o.stop_price}")

    at_new = [
        o for o in stops2
        if o.stop_price is not None and Decimal(str(o.stop_price)) == quant
    ]
    # PASS requires: a protective stop present both before (proven above) AND
    # after, exactly one live stop now, and it sits at the new price. Because
    # there was ≥1 stop before and ≥1 after, the position was never observed
    # unprotected; Alpaca's replace is a single atomic server op (the original
    # goes to `replaced`, never a dangling cancel-with-no-replacement).
    if result and len(stops2) == 1 and len(at_new) == 1:
        print(
            f"\n✓ CHECK #2 PASSES: HELD stop replaced to {quant}; exactly one live "
            "stop remains and the position was continuously protected. "
            "replace_stop_price is safe on a HELD bracket leg."
        )
        verdict = 0
    else:
        print(
            f"\n✗ CHECK #2 FAILED/UNCERTAIN: result={result}, live_stops_after="
            f"{len(stops2)}, at_new_price={len(at_new)}. Inspect the dump above "
            "before relying on the trailing path."
        )
        verdict = 2

    _cleanup(broker, symbol)
    return verdict


def main(argv=None) -> int:
    args = _parse_args(argv)
    broker = _broker()
    symbol = args.symbol.upper()

    if args.cleanup_only:
        _cleanup(broker, symbol)
        return 0

    if args.check_replace:
        try:
            return _check_replace(broker, symbol, args.qty)
        except Exception:
            # Never leave a probe position dangling on an unexpected error.
            print("unexpected error during check #2 — forcing cleanup")
            _cleanup(broker, symbol)
            raise

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
        # Alpaca enforces client_order_id uniqueness permanently (not just over
        # open orders), so a fixed id makes this probe single-use: every rerun
        # collides with 40010001 "client_order_id must be unique". Append a
        # unique suffix; cleanup works by symbol, so the readable prefix is only
        # for spotting the order in the dashboard.
        client_order_id=f"probe-overnight-{symbol}-{uuid.uuid4().hex[:8]}",
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
        # Not every 422 is a verdict on the order *shape*. A duplicate
        # client_order_id (code 40010001) means this id was already used — the
        # bracket was never even evaluated — so it says nothing about whether
        # GTC market brackets are supported. Don't misreport it as a check #1
        # failure. (The id is now uuid-suffixed, so this should not recur.)
        msg = str(exc)
        if "40010001" in msg or "client_order_id must be unique" in msg:
            print(
                "\n⚠ INCONCLUSIVE: submission rejected on a duplicate "
                f"client_order_id, not on the bracket shape:\n  {exc}\n"
                "  → This is NOT a check #1 result. Re-run; the id is now "
                "uniquified per run."
            )
            return 3
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
