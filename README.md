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

## Overnight holding (experimental, opt-in)

By default every strategy flattens before the close. Overnight holding lets a
position be carried past the close, protected by a broker-side GTC stop/take
bracket. It is gated behind `--protect-overnight` and ships **disabled** — the
commands below verify it works on your Alpaca paper account before you turn it
on. All of them need `ALPACA_API_KEY`/`ALPACA_API_SECRET` in `.env` and
`TRADING_MODE=paper` (the default).

**Check 1 — does Alpaca accept a GTC market bracket? (run during market hours)**

```bash
uv run python scripts/probe_overnight_protection.py
```

Buys 1 share of SPY with a GTC stop/take bracket placed 10% either side of the
last trade (so neither leg fires), routed through the risk layer, prints whether
Alpaca accepted it and the legs' time-in-force, then closes the position and
cancels the orders. A `✓ … check #1 PASSES` line means GTC brackets work; a
`✗ CHECK #1 FAILED` line means market+GTC is unsupported and the feature needs
its fallback before use. This is the gate for everything below.

**Check 2 & 3 — do the legs survive overnight / is the entry price stable?**

```bash
uv run python scripts/probe_overnight_protection.py --keep
```

Same probe, but `--keep` leaves the position and its GTC legs open instead of
cleaning up. The next morning, before the open, confirm on the Alpaca dashboard
that the stop/take legs are still present and `avg_entry_price` is unchanged,
then tear it down:

```bash
uv run python scripts/probe_overnight_protection.py --cleanup-only --symbol SPY
```

Cancels any open orders for the symbol and closes its position — the teardown
for a `--keep` run.

**Check 4 — full round-trip with the real bot**

```bash
uv run python scripts/run_paper.py --protect-overnight
```

Runs a paper session with overnight holding on: entries are submitted GTC, open
positions are recorded to `data/ops/carried/` on a clean exit, and matching
positions are adopted on the next start instead of being refused. To actually
carry a position overnight, also set `flat_by_et: null` in that strategy's
`config.yaml` (e.g. `src/trading_bot/strategy/orb/config.yaml`) so it stops
emitting the 15:55 flat. Stop the bot cleanly (Ctrl-C) with a position open,
confirm a `CARRIED_*.json` file appears, then relaunch with the same flag and
confirm the log shows `adopting carried overnight positions`.

> Keep `--protect-overnight` and `flat_by_et: null` off until check 1 passes on
> live paper — until then a held position would be unprotected after the close.
