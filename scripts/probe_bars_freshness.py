"""Probe how fresh Alpaca's bars are right now.

Run in a second terminal while the bot is running:
    uv run python scripts/probe_bars_freshness.py

Prints, for a handful of liquid names, the timestamp of the most
recent bar returned by the same code path the session loop uses.
Use to distinguish "data feed is stale" from "strategy is buggy".
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

from trading_bot.execution import AlpacaPaperBroker


def main() -> int:
    load_dotenv()
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not sec:
        print("ALPACA_API_KEY / ALPACA_API_SECRET not set")
        return 1

    broker = AlpacaPaperBroker(api_key=key, api_secret=sec)
    now = datetime.now(UTC)
    end = now - timedelta(minutes=1)
    start = end - timedelta(hours=6)

    print(f"now (UTC):   {now.isoformat()}")
    print(f"fetch end:   {end.isoformat()}")
    print(f"fetch start: {start.isoformat()}")
    print("-" * 64)
    print(f"{'symbol':<8} {'rows':>6}  last bar (UTC)               age")
    print("-" * 64)
    for sym in ("SPY", "QQQ", "NVDA", "TSLA", "AMZN", "JPM", "ARM", "WSBC"):
        df = broker.get_recent_bars(sym, start, end)
        if len(df) == 0:
            print(f"{sym:<8} {0:>6}  EMPTY")
            continue
        last = df.index[-1]
        age = now - last.to_pydatetime()
        age_min = age.total_seconds() / 60
        print(f"{sym:<8} {len(df):>6}  {last.isoformat()}  {age_min:6.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
