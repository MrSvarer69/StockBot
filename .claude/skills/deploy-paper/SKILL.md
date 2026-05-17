---
name: deploy-paper
description: Launch the bot against Alpaca paper trading for a session. Use to start a live paper run, not a backtest. Refuses if TRADING_MODE is not "paper".
---

# Deploy (paper)

Start the bot against Alpaca's paper trading API.

## Preflight (in order — abort on first failure)

1. Check `.env` exists and `ALPACA_API_KEY` + `ALPACA_API_SECRET` are non-empty.
2. Check `TRADING_MODE=paper` in `.env`. If `live`, **stop and tell the user this
   skill does not deploy live; that requires `risk-officer` review**.
3. Check the working tree is clean (`git status --porcelain` is empty). If not, ask
   whether to proceed with uncommitted changes.
4. Run the test suite: `uv run pytest -q`. Abort if any test fails.
5. Confirm the symbol universe and strategy with the user before launching.

## Launch

```
uv run python -m trading_bot.ops.run --strategy <name> --symbols <list>
```

Stream the first 30 seconds of logs to confirm the heartbeat fires and the broker
connection is established. Then hand control back to the user with the PID and the
log path.

## Output

Report PID, log path, strategy + symbol list, and the kill command:
`kill <PID>` or `touch data/KILL` (the manual kill file).
