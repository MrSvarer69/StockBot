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
from pathlib import Path

from dotenv import load_dotenv

from trading_bot.contracts import RiskParams
from trading_bot.data import load_prior_session_bars
from trading_bot.data.insider import (
    EdgarClient,
    Form4Cache,
    TickerMap,
    refresh_cache_to_today,
)
from trading_bot.execution import AlpacaPaperBroker, SessionConfig, run_session
from trading_bot.ops import setup_logging
from trading_bot.strategy import ORBStrategy, load_config
from trading_bot.strategy.composite import CompositeStrategy
from trading_bot.strategy.insider import InsiderStrategy
from trading_bot.strategy.insider import load_config as load_insider_config
from trading_bot.strategy.pullback import PullbackStrategy
from trading_bot.strategy.pullback import load_config as load_pullback_config


# Broad universe: every cached symbol with one explicit exclusion.
#
# MARA is excluded after the 2026-05-20 backtest: PF 0.68 at 1 bp, 0.49 at
# 3 bps, and a bar-scale ATR of ~$0.025 that makes fills unreliable even on
# paper. AMD/COIN/MSTR/NVDA passed strongly on the same backtest; AAPL/JPM/
# NVDA passed on the original 2025-11-15 -> 2026-05-13 backtest. All other
# symbols below have not been individually backtested -- the picker is
# trusted to rank them by OR/ATR each morning and only fire the top
# --max-concurrent-entries.
_WIDE_UNIVERSE_DEFAULT = (
    # Small-bankroll universe: every name kept here had a 2026-05-26
    # observed price under ~$75/share in this session's run logs, so a
    # $75 per-trade notional ceiling (10% of a $750 capital cap) sizes
    # to at least 1 whole share. Higher-priced names from the prior wide
    # universe (SPY, AAPL, NVDA, GS, MU, GLD, AMD, AVGO, TSLA, META, PLTR,
    # BABA, etc.) are intentionally excluded — at $750 capped equity they
    # round to qty=0 every time.
    #
    # Sector ETFs and high-vol momentum names in the affordable band:
    "XLE,RIOT,SMCI,"
    # Insider-track names confirmed sub-$75 from observed prices.
    "EMN,MTDR,COO,NSP,"
    # Insider-track regional banks (mid-cap NYSE/NASDAQ); typically priced
    # in the $20-$60 band suitable for whole-share sizing on a $750 cap.
    "CBC,WSBC,GABC,FMBM,SFNC,BWFG,CIVB,MIAX"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Paper-trade the ORB strategy via Alpaca.")
    p.add_argument(
        "--symbols",
        default=_WIDE_UNIVERSE_DEFAULT,
        help=(
            "Comma-separated list of symbols. Default is a 56-name broad "
            "universe across index/sector ETFs, AI/semis, mega-cap tech, "
            "financials, crypto-adjacent and high-vol names, consumer/"
            "industrial/pharma diversifiers, and insider-track regional "
            "banks + others with high Form 4 signal density. MARA is "
            "explicitly excluded (failed the 2026-05-20 backtest: PF 0.68 "
            "+ microstructure). The cross-symbol picker selects the top "
            "--max-concurrent-entries by OR/ATR ratio each iteration."
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
        "--max-capital-usd",
        type=Decimal,
        default=None,
        help=(
            "Hard cap on the equity used for sizing and percent-based risk "
            "caps. Set when the paper account ($100k) is larger than the "
            "real-world bankroll you intend to deploy. Currency is USD; "
            "convert from local currency externally (e.g. 3000 DKK at "
            "~6.9 DKK/USD ≈ $435). Default: no cap (raw broker equity used)."
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
        "--flatten-on-exit",
        action="store_true",
        help=(
            "Close all open positions when the session exits cleanly (manual "
            "Ctrl-C / SIGTERM, market close). Default off: positions roll "
            "across restarts and the broker-side bracket children keep "
            "guarding them. Halt-class exits (kill switch, broker auth "
            "errors, consecutive failures) always flatten regardless."
        ),
    )
    p.add_argument(
        "--no-flatten-on-halt",
        action="store_true",
        help=(
            "Disable the flatten-on-halt safety reflex. Strongly discouraged: "
            "a halt means something has gone wrong, and leaving positions "
            "open removes the bot's last chance to close them. Provided only "
            "for operators who explicitly accept that risk."
        ),
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
    p.add_argument(
        "--no-orb",
        action="store_true",
        help=(
            "Disable the ORB strategy in the composite. Diagnostic only -- "
            "use to run an insider-only session."
        ),
    )
    p.add_argument(
        "--no-insider",
        action="store_true",
        help=(
            "Disable the Form 4 insider strategy in the composite. "
            "Diagnostic only -- use to run an ORB-only session."
        ),
    )
    p.add_argument(
        "--no-midday",
        action="store_true",
        help=(
            "Disable the pullback-to-EMA midday strategy in the composite. "
            "Diagnostic only -- use to run a session without midday cover."
        ),
    )
    p.add_argument(
        "--insider-refresh-minutes",
        type=int,
        default=15,
        help=(
            "How often (minutes) to pull new Form 4 filings from EDGAR "
            "during a session and rebuild the insider signal index. "
            "Default 15. Set to 0 to disable in-session refresh (the "
            "startup pre-flight still runs). Anti-lookahead: new filings "
            "captured mid-session affect tomorrow's signals, not today's."
        ),
    )
    p.add_argument(
        "--no-insider-preflight",
        action="store_true",
        help=(
            "Skip the startup refresh of the Form 4 cache. Default: at "
            "launch, pull anything new from EDGAR for the last 7 days so "
            "today's insider signals reflect filings made since the last "
            "session. Use this flag only when offline or when EDGAR is "
            "known to be unreachable."
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

    if args.no_orb and args.no_insider and args.no_midday:
        print(
            "ERROR: --no-orb, --no-insider, and --no-midday were all set; "
            "nothing to run.",
            file=sys.stderr,
        )
        return 2
    inner_strategies: list = []
    inner_names: list[str] = []
    # Per-strategy trailing-stop policy. Keyed by the value the strategy emits
    # in the `strategy` column of signal rows ("orb" | "pullback" | "insider"),
    # which is what the session uses to look up trail enablement at entry.
    trailing_stop_policy: dict[str, bool] = {}
    if not args.no_orb:
        orb_strategy = ORBStrategy(load_config())
        trailing_stop_policy["orb"] = orb_strategy.config.enable_trailing_stop
        inner_strategies.append(orb_strategy)
        inner_names.append("orb")
    if not args.no_midday:
        # Pre-warm EMA buffers with the most recent prior session(s) so the
        # pullback can fire from 09:30 ET instead of waiting ~200 minutes
        # mid-session. Gated by PullbackConfig.warmup_from_prior_session.
        pullback_cfg = load_pullback_config()
        pullback_strategy = PullbackStrategy(pullback_cfg)
        if pullback_cfg.warmup_from_prior_session:
            today_utc = datetime.now(UTC)
            prior_by_symbol = {
                sym: load_prior_session_bars(sym, today_utc, min_bars=200)
                for sym in symbols
            }
            pullback_strategy.set_prior_session_bars(prior_by_symbol)
        trailing_stop_policy["pullback"] = pullback_cfg.enable_trailing_stop
        inner_strategies.append(pullback_strategy)
        inner_names.append("midday")

    refresh_hook = None
    refresh_interval_minutes: int | None = None
    insider_strategy: InsiderStrategy | None = None
    if not args.no_insider:
        # Pre-flight: pull anything new from EDGAR into the parquet cache
        # BEFORE constructing the strategy. This is what guarantees the
        # in-memory signal index at session start reflects filings made
        # since the last backfill. Without this, a stale cache silently
        # produces 0 signals for today (the failure mode observed
        # 2026-05-20).
        edgar_client = EdgarClient(cache_dir=Path("data/insider_raw"))
        form4_cache = Form4Cache(root=Path("data/insider"))
        ticker_map = TickerMap.from_client(edgar_client)
        if not args.no_insider_preflight:
            log = logging.getLogger(__name__)
            log.info("insider pre-flight refresh starting (last 7 days)")
            try:
                refresh_cache_to_today(
                    client=edgar_client,
                    cache=form4_cache,
                    ticker_map=ticker_map,
                )
            except Exception:
                log.exception(
                    "insider pre-flight refresh failed; continuing with "
                    "existing cache. Today's insider signals may be stale."
                )
        insider_strategy = InsiderStrategy(load_insider_config())
        trailing_stop_policy["insider"] = insider_strategy.config.enable_trailing_stop
        inner_strategies.append(insider_strategy)
        inner_names.append("insider")

        if args.insider_refresh_minutes > 0:
            refresh_interval_minutes = args.insider_refresh_minutes

            def refresh_hook() -> None:
                """Pull new filings from EDGAR, then rebuild the index."""
                refresh_cache_to_today(
                    client=edgar_client,
                    cache=form4_cache,
                    ticker_map=ticker_map,
                )
                if insider_strategy is not None:
                    insider_strategy.reload()

    strategy = CompositeStrategy(inner_strategies, names=inner_names)
    # Per-trade risk cap. Default 2% — the conservative guardrail tuned for a
    # full $100k account, kept as an independent limit below target sizing.
    # When --max-capital-usd bounds absolute dollar exposure, loosen to 10% so
    # a small bankroll can size whole shares of sub-$75 names (2% of $750 = $15
    # rounds every share to zero). The loosened cap is applied ONLY when a
    # capital cap is active, so it can never widen single-trade exposure
    # against the full uncapped paper equity.
    max_pct_per_trade = (
        Decimal("0.10") if args.max_capital_usd is not None else Decimal("0.02")
    )
    risk_params = RiskParams(
        max_pct_per_trade=max_pct_per_trade,
        max_daily_loss_pct=Decimal("0.03"),
        max_daily_notional_pct=Decimal("0.50"),
        max_position_count=args.max_position_count,
        symbol_whitelist=frozenset(symbols),
        max_capital_usd=args.max_capital_usd,
    )
    if args.max_capital_usd is not None:
        logging.getLogger(__name__).info(
            "capital cap active: sizing and percent-based risk caps apply to "
            "min(account_equity, $%s)",
            args.max_capital_usd,
        )
    picker_min_ratio = (
        args.picker_min_ratio if args.picker_min_ratio > 0 else None
    )
    config = SessionConfig(
        symbols=symbols,
        poll_interval_seconds=args.poll,
        max_iterations=args.max_iterations,
        flatten_on_exit=args.flatten_on_exit,
        flatten_on_halt=not args.no_flatten_on_halt,
        force_ignore_unreconciled=args.force_unreconciled,
        max_concurrent_entries=args.max_concurrent_entries,
        picker_min_ratio=picker_min_ratio,
        run_id=run_id,
        refresh_interval_minutes=refresh_interval_minutes,
        refresh_hook=refresh_hook,
        trailing_stop_policy=trailing_stop_policy,
    )

    run_session(broker, strategy, config, risk_params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
