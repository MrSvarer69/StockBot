# Trading Bot

Paper-trading sandbox. See `CLAUDE.md` for ground rules and `scripts/run_paper.py --help`
for the full flag set.

## Running a paper session

Paper-trade with a $750 hard capital cap, flattening positions on a clean exit:

```bash
uv run python scripts/run_paper.py \
    --max-capital-usd 750 \
    --flatten-on-exit
```

> The `\` at the end of each line is required. Without the backslashes the shell runs
> only `uv run python scripts/run_paper.py` and silently drops the remaining flags — so
> the capital cap would never take effect. On launch, confirm the
> `capital cap active: ...` line appears in the log.

Paper-trade a custom symbol list (the default is a 56-name universe):

```bash
uv run python scripts/run_paper.py --symbols NVDA,QQQ,AMD,MU,COIN,ORCL,SMCI
```
