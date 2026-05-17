"""Run the ORB strategy against Alpaca paper trading.

Usage:
  uv run python scripts/run_paper.py --symbols SPY
  uv run python scripts/run_paper.py --symbols SPY,QQQ --poll 60

Hard gate:
  Refuses to run if TRADING_MODE is set to anything other than "paper" (the
  default). Live trading would require a different entrypoint and risk-officer
  approval.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal

from dotenv import load_dotenv

from trading_bot.contracts import RiskParams
from trading_bot.execution import AlpacaPaperBroker, SessionConfig, run_session
from trading_bot.ops import setup_logging
from trading_bot.strategy import ORBStrategy, load_config


_WIDE_UNIVERSE_DEFAULT = (
    # Index ETFs
    "SPY,QQQ,IWM,"
    # AI / semis (historically strongest fit for ORB at the shared config)
    "NVDA,AMD,MU,AVGO,SMCI,MRVL,ARM,"
    # Mega-cap tech / software
    "AAPL,MSFT,META,GOOGL,AMZN,NFLX,ORCL,CRWD,"
    # Financials
    "JPM,GS,"
    # Crypto-adjacent / high-vol
    "COIN,MSTR,"
    # Other diversifiers
    "ABNB,BA,LLY"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Paper-trade the ORB strategy via Alpaca.")
    p.add_argument(
        "--symbols",
        default=_WIDE_UNIVERSE_DEFAULT,
        help=(
            "Comma-separated list of symbols. Default is a 24-name wide "
            "universe across index ETFs, AI/semis, mega-cap tech, financials, "
            "and crypto-adjacent names. The cross-symbol picker selects the "
            "top --max-concurrent-entries by OR/ATR ratio each iteration."
        ),
    )
    p.add_argument(
        "--max-concurrent-entries",
        type=int,
        default=5,
        help=(
            "Cap on NEW entries fired per iteration after the picker ranks "
            "candidates by OR/ATR ratio. Default 5 matches max_position_count."
        ),
    )
    p.add_argument(
        "--max-position-count",
        type=int,
        default=5,
        help=(
            "Total open-position cap enforced at the risk layer. Default 5. "
            "With target_size_pct=10%%, 5 positions = 50%% notional (matches "
            "max_daily_notional_pct)."
        ),
    )
    p.add_argument(
        "--picker-min-ratio",
        type=float,
        default=0.5,
        help=(
            "Pool-entry OR/ATR ratio floor for the picker. Candidates below "
            "this are excluded before ranking. Default 0.5 — matches the "
            "strategy's own filter floor so 'passed strategy filter' implies "
            "'eligible for picker ranking'. Raise (e.g. 1.0) to demand "
            "higher-conviction setups; set to 0 to disable."
        ),
    )
    p.add_argument(
        "--poll",
        type=int,
        default=60,
        help="Poll interval in seconds (default: 60)",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Stop after this many iterations (default: unlimited)",
    )
    p.add_argument(
        "--no-flatten",
        action="store_true",
        help="Skip flatten-on-exit. Default is to flatten.",
    )
    p.add_argument(
        "--force-unreconciled",
        action="store_true",
        help=(
            "Start even if a previous run left UNRECONCILED_*.json sentinel "
            "files. Use only after manually reconciling open positions with "
            "the broker."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()

    mode = os.environ.get("TRADING_MODE", "paper")
    if mode != "paper":
        print(
            f"ERROR: TRADING_MODE={mode!r}. This script runs paper only. "
            "Live trading is intentionally gated elsewhere.",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("ALPACA_API_KEY", "")
    api_secret = os.environ.get("ALPACA_API_SECRET", "")
    if not api_key or not api_secret:
        print(
            "ERROR: set ALPACA_API_KEY and ALPACA_API_SECRET in .env "
            "(paper keys: https://app.alpaca.markets/paper/dashboard/overview)",
            file=sys.stderr,
        )
        return 1

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = setup_logging(run_id=run_id, level=logging.INFO)
    logging.getLogger(__name__).info("audit log: %s", log_path)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    broker = AlpacaPaperBroker(api_key=api_key, api_secret=api_secret)
    strategy = ORBStrategy(load_config())
    risk_params = RiskParams(
        max_pct_per_trade=Decimal("0.02"),
        max_daily_loss_pct=Decimal("0.03"),
        max_daily_notional_pct=Decimal("0.50"),
        max_position_count=args.max_position_count,
        symbol_whitelist=frozenset(symbols),
    )
    picker_min_ratio = (
        args.picker_min_ratio if args.picker_min_ratio > 0 else None
    )
    config = SessionConfig(
        symbols=symbols,
        poll_interval_seconds=args.poll,
        max_iterations=args.max_iterations,
        flatten_on_exit=not args.no_flatten,
        force_ignore_unreconciled=args.force_unreconciled,
        max_concurrent_entries=args.max_concurrent_entries,
        picker_min_ratio=picker_min_ratio,
        run_id=run_id,
    )

    run_session(broker, strategy, config, risk_params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
