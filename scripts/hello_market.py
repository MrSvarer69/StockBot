"""Smoke test: pull yesterday's 1-minute bars for a symbol from Alpaca and print a summary.

Run with: uv run python scripts/hello_market.py
Requires: .env with ALPACA_API_KEY and ALPACA_API_SECRET (paper keys are fine).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()

    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        print("ERROR: set ALPACA_API_KEY and ALPACA_API_SECRET in .env", file=sys.stderr)
        print("Get paper keys at https://app.alpaca.markets/paper/dashboard/overview", file=sys.stderr)
        return 1

    symbol = os.environ.get("SYMBOLS", "SPY").split(",")[0].strip()

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError:
        print("ERROR: alpaca-py not installed. Run: uv sync", file=sys.stderr)
        return 1

    client = StockHistoricalDataClient(api_key, api_secret)

    end = datetime.now(UTC) - timedelta(minutes=20)
    start = end - timedelta(days=5)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
    )

    bars = client.get_stock_bars(req).df
    if bars.empty:
        print(f"No bars returned for {symbol} in the last 5 days.")
        return 1

    last_session = bars.index.get_level_values("timestamp").normalize().unique()[-1]
    session_bars = bars[bars.index.get_level_values("timestamp").normalize() == last_session]

    print(f"Connected to Alpaca. Pulled {len(bars)} bars for {symbol}.")
    print(f"Last full session: {last_session.date()} ({len(session_bars)} bars).")
    print(f"  open  : {session_bars['open'].iloc[0]}")
    print(f"  high  : {session_bars['high'].max()}")
    print(f"  low   : {session_bars['low'].min()}")
    print(f"  close : {session_bars['close'].iloc[-1]}")
    print(f"  volume: {int(session_bars['volume'].sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
