---
name: replay-trade
description: Reconstruct the decision that led to a specific trade from the logs. Use when a trade looks wrong and you want to understand what the bot saw at the moment of decision.
---

# Replay trade

Given a trade ID or a timestamp + symbol, reconstruct everything the bot knew when
it made the decision.

## Inputs

- **trade_id** OR (**timestamp** + **symbol**) — required

## Procedure

1. Locate the decision log entry in `data/logs/decisions/` by trade ID or by
   timestamp + symbol nearest match.
2. Load the bar window the strategy used (config-defined lookback) up to the
   decision timestamp.
3. Re-run the strategy's pure decision function on that exact input.
4. Compare the replayed output to what was actually executed. Flag any mismatch.
5. Produce a Markdown report containing:
   - The input bars (as a table)
   - The signal output
   - The risk-officer's sizing decision
   - The actual order sent and broker response
   - Any discrepancy between replay and reality

## Output

Path to the replay report. Inline summary: "match" or a description of the
mismatch.

## When to use this

- Trade looked wrong in hindsight — was the logic wrong, or the data?
- Suspected bug in signal generation.
- Investigating a fill at an unexpected price (slippage vs logic issue).
