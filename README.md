  uv run python scripts/run_paper.py \
      --max-capital-usd 750 \
      --stale-entry-window 120

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

## Overnight holding (default on)

The paper bot **holds positions overnight by default**. Entries are submitted
GTC so the broker-side stop/take bracket survives the close, the intraday 15:55
ET flat is dropped for `orb`/`pullback`, open positions are recorded to
`data/ops/carried/` on a clean exit, and matching positions are adopted on the
next start instead of being refused. `insider` already carries to its holding
horizon; the only change there is that its bracket is now GTC (protected past
the close) rather than a DAY bracket that expired at 16:00 ET.

To restore the old flatten-before-close behavior (DAY brackets, 15:55 ET flat,
no carry), pass `--no-protect-overnight`:

```bash
uv run python scripts/run_paper.py --no-protect-overnight
```

Note: `config.yaml`'s `flat_by_et: "15:55"` is unchanged — it is the
flatten-EOD value used by **backtests** and by `--no-protect-overnight`. The
live runner overrides it to `null` when overnight holding is on; backtests stay
intraday.

The commands below were used to verify GTC brackets work on Alpaca paper before
this became the default. They remain useful for re-checking on a new account.
All of them need `ALPACA_API_KEY`/`ALPACA_API_SECRET` in `.env` and
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
uv run python scripts/run_paper.py
```

Runs a normal paper session (overnight holding on by default): entries are
submitted GTC, the intraday flat is dropped for `orb`/`pullback`, open positions
are recorded to `data/ops/carried/` on a clean exit, and matching positions are
adopted on the next start instead of being refused. Stop the bot cleanly
(Ctrl-C) with a position open, confirm a `CARRIED_*.json` file appears, then
relaunch and confirm the log shows `adopting carried overnight positions`.

> If GTC brackets are ever rejected on a new account (check 1 fails), run with
> `--no-protect-overnight` until the broker side is sorted — otherwise a held
> position would be unprotected after the close.
