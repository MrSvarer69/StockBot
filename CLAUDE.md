# Trading Bot — project ground rules for Claude

This repo is a sandbox: paper-trading first, used as a substrate for experimenting with
Claude agents and skills. Engineering quality matters more than alpha.

## Hard rules

1. **Paper-only by default.** All code paths must default to Alpaca paper trading. A live
   trading path may exist, but switching to it must require an explicit, non-default
   `TRADING_MODE=live` environment variable AND pass review by the `risk-officer` agent.
2. **No forecasts in outputs.** Do not produce or commit projected returns, P&L estimates,
   "expected" win rates, or any other speculative financial figures. Backtest *results*
   computed from real historical data are fine. Forward-looking numeric claims are not.
3. **No secrets in the repo.** API keys live in `.env` (gitignored). `.env.example`
   documents the variable names only.
4. **Risk boundary is non-optional.** Every order placement path must go through
   `trading_bot.risk` for sizing and sanity checks. No direct calls to the broker client
   from strategy code.
5. **Git handling.** ALL git handling will be handled by me. Meaning NO git commit
   or git push.

## Layout

```
src/trading_bot/
  data/        # market data ingestion + storage
  strategy/    # signal generation (pure functions, no I/O)
  backtest/    # historical simulation engine
  risk/        # position sizing, kill-switch, sanity checks
  execution/   # broker adapters (paper, live)
  ops/         # logging, monitoring, deployment helpers
scripts/       # one-off runnable entry points
tests/         # pytest suite
data/          # local data cache (gitignored content)
```

## Conventions

- Python 3.12+, managed by `uv`. Run `uv sync` after pulling.
- Pure functions in `strategy/` — they take a DataFrame, return signals. No side effects.
- All times in UTC internally. Convert to ET only at the display boundary.
- Use `decimal.Decimal` for any quantity that maps to currency; never `float`.

## Agents

Five specialized subagents live in `.claude/agents/`:

- `data-engineer` — ingestion, schemas, backfills
- `strategist` — signal design, parameter exploration
- `backtester` — runs simulations, reports metrics
- `risk-officer` — reviews any change touching `execution/` or `risk/`; gates live mode
- `ops` — deployment, logging, monitoring

## Skills

Repeatable workflows in `.claude/skills/`:

- `/backtest` — run a backtest with given strategy + params
- `/deploy-paper` — package and launch the bot against Alpaca paper
- `/replay-trade` — reconstruct a single trade decision from logs

## What this project is for

This project is for learning Claude agents + building a trading bot that can be used 
as a small sidehustle. Profit from this is NOT a priority, only a nice thing.