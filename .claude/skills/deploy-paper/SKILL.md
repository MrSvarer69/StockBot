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
4. Run the test suite: `uv run python -m pytest -q`. Abort if any test fails.
5. Confirm with the user before launching: the symbol universe (`--symbols`, or the
   default 56-name universe), which strategies run (all three by default; disable with
   `--no-orb` / `--no-insider` / `--no-midday`), and — if the paper account is larger
   than the bankroll they intend to deploy — the **capital cap** (`--max-capital-usd`).
   Without `--max-capital-usd`, sizing uses the full broker equity and total exposure
   is unbounded.

## Launch

The runnable entry point is `scripts/run_paper.py` (there is no `trading_bot.ops.run`
module). Strategy selection is by opt-out flags, not a `--strategy` argument.

```
uv run python scripts/run_paper.py \
    --symbols <comma,separated,list> \
    --max-capital-usd <usd> \
    --flatten-on-exit
```

Every flag is optional; run `uv run python scripts/run_paper.py --help` for the full
set. IMPORTANT: when handing the user a multi-line command, keep the `\`
line-continuations — without them the shell runs only the first line and silently
drops the remaining flags (this is what caused a `--max-capital-usd` cap to be
ignored in a prior run).

Stream the first 30 seconds of logs to confirm the heartbeat fires and the broker
connection is established. When `--max-capital-usd` is set, also confirm the
`capital cap active: ...` line appears — its absence means the cap did not take
effect. Then hand control back to the user with the PID and the log path.

## Output

Report PID, log path, strategy + symbol list, whether a capital cap is active, and
the kill command: `kill <PID>` or `touch data/ops/KILL` (the manual kill file — the
default `kill_file_path`).
