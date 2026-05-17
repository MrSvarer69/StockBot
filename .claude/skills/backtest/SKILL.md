---
name: backtest
description: Run a backtest of a named strategy against a historical date range and produce a metrics report. Use when iterating on a strategy or evaluating a parameter change.
---

# Backtest

Run a strategy against historical bars and produce a metrics report.

## Inputs

Parse from the invocation args (free-form text — extract what's present):

- **strategy** (required) — name matching a module under `src/trading_bot/strategy/`
- **symbol** or **symbols** (required) — e.g. `SPY` or `SPY,QQQ`
- **start** / **end** (optional) — ISO dates; default last 6 months
- **params** (optional) — overrides for the strategy's `config.yaml`

If a required input is missing, ask the user for it. Do not invent defaults for
strategy or symbol.

## Procedure

1. Verify the strategy module exists at `src/trading_bot/strategy/<name>/`.
2. Confirm historical data is cached for the date range; if not, fetch via
   `trading_bot.data.history.ensure_cached(symbols, start, end)`.
3. Run `python -m trading_bot.backtest.run --strategy <name> --symbols ... --start ... --end ...`.
4. Read the generated report at `data/backtests/<run-id>/report.md` and summarize:
   - Total return, Sharpe, max drawdown, win rate
   - Number of trades, average holding period
   - A sentence about any obvious red flags (e.g. drawdown > 30%, win rate < 30%)
5. Do **not** extrapolate to forward-looking forecasts. Report on the historical
   period only.

## Output

Reply with the summary inline, and the path to the full report.
